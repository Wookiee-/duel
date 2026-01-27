import os
import time
import re
import socket
import sys
import configparser
import sqlite3
import math
import threading

def normalize(text):
    if not text: return ""
    clean = re.sub(r'\^.', '', text)
    clean = clean.replace("^7", "").strip()
    return " ".join(clean.split())

class Player:
    def __init__(self, sid, name, guid, rating=1500, rd=350, vol=0.06, clan="NONE", role="MEMBER", group="DEFAULT"):
        self.id = sid
        self.name = name
        self.guid = guid
        self.rating = rating
        self.rd = rd
        self.vol = vol
        self.clan_tag = clan
        # Hierarchy: MEMBER (0) < OFFICER (1) < LEADER (2) < OWNER (3)
        self.role = role  
        self.clan_group = group 
        self.match_score = 0
        self.opponent = None
        self.is_paused = False
        self.pending_invite_from = None 
        self.pending_limit = 5
        self.pending_group_request = None

    @property
    def clean_name(self):
        return normalize(self.name).lower()

class MBIIDuelPlugin:
    def __init__(self):
        self.config_file = sys.argv[1] if len(sys.argv) > 1 else 'duel.cfg'
        self.settings = {}
        self.players = []
        self.load_config()
        self.db_filename = self.settings.get('db_file', 'duel.db')
        self.init_sqlite()

        self.lobby_open = False
        self.lobby_players = []
        self.active_tournament = False
        self.tournament_paused = False
        self.is_cvc = False 
        self.current_round_matches = [] 
        self.round_winners = []
        self.win_limit = 5
        self.tournament_round_num = 1
        self.locked_groups = {}
        
        # Load any existing progress from previous map/round
        self.restore_match_progress()

    def init_sqlite(self):
        with sqlite3.connect(self.db_filename, timeout=20) as conn:
            cursor = conn.cursor()
            cursor.execute('''CREATE TABLE IF NOT EXISTS players (
                    guid TEXT PRIMARY KEY, name TEXT, clean_name TEXT, clan_tag TEXT DEFAULT 'NONE',
                    clan_role TEXT DEFAULT 'MEMBER', clan_group TEXT DEFAULT 'DEFAULT',
                    duel_rating REAL DEFAULT 1500, rating_deviation REAL DEFAULT 350,
                    total_rounds_won INTEGER DEFAULT 0, tournament_wins INTEGER DEFAULT 0)''')
            
            # Persistent state for map restarts
            cursor.execute('''CREATE TABLE IF NOT EXISTS active_matches (
                    p1_guid TEXT, p2_guid TEXT, p1_score INTEGER, p2_score INTEGER, 
                    win_limit INTEGER, is_cvc INTEGER)''')
            conn.commit()

    def load_config(self):
        config = configparser.ConfigParser()
        config.read(self.config_file)
        self.settings = dict(config['SETTINGS'])

    def save_match_progress(self, p1, p2):
        """Saves current scores to DB to survive MB2's 15-minute round limit."""
        with sqlite3.connect(self.db_filename) as conn:
            conn.execute("DELETE FROM active_matches WHERE (p1_guid=? AND p2_guid=?) OR (p1_guid=? AND p2_guid=?)",
                         (p1.guid, p2.guid, p2.guid, p1.guid))
            conn.execute("INSERT INTO active_matches (p1_guid, p2_guid, p1_score, p2_score, win_limit, is_cvc) VALUES (?, ?, ?, ?, ?, ?)",
                         (p1.guid, p2.guid, p1.match_score, p2.match_score, self.win_limit, int(self.is_cvc)))

    def restore_match_progress(self):
        """Called at startup to see if we were in the middle of a match."""
        pass # Logic handled during player sync to re-link opponents

    def detect_clan(self, name):
        match = re.search(r'^[\[<\(](.*?)[\]>\)]|^(.*?)\s?\|', normalize(name))
        return match.group(1).strip().upper() if match and match.group(1) else "NONE"

    def calculate_glicko2(self, winner, loser):
        def g(rd): return 1 / math.sqrt(1 + 3 * (rd**2) / (math.pi**2))
        def E(r1, r2, rd2): return 1 / (1 + math.exp(-g(rd2) * (r1 - r2) / 173.7178))
        r1, rd1 = (winner.rating - 1500) / 173.7178, winner.rd / 173.7178
        r2, rd2 = (loser.rating - 1500) / 173.7178, loser.rd / 173.7178
        v = 1 / (g(rd2)**2 * E(r1, r2, rd2) * (1 - E(r1, r2, rd2)))
        new_rd1 = 1 / math.sqrt(1 / rd1**2 + 1 / v)
        new_r1 = r1 + new_rd1**2 * (g(rd2) * (1 - E(r1, r2, rd2)))
        winner.rating = 1500 + 173.7178 * new_r1
        winner.rd = max(30, 173.7178 * new_rd1)

    def handle_smod_command(self, raw_admin_name, admin_id, full_message):
        msg_parts = full_message.split()
        if not msg_parts: return
        command = msg_parts[0].lower().replace("!", "")

        if command == "dhelp":
            self.send_rcon(f'svtell {admin_id} "^5Admin Ops: ^7!clan <name> <tag>, !group <name> <group>, !promote <name>, !resetplayer <name>, !cstart, !tstart, !tpause, !tresume"')

        elif command == "cstart":
            if len(msg_parts) > 1 and msg_parts[1] == "cancel":
                self.active_tournament = self.is_cvc = False
                self.send_rcon('say "^5[CvC] ^1Clan Match Cancelled by Admin."')
            else:
                self.lobby_open, self.is_cvc = True, True
                self.send_rcon('say "^5[CvC] ^7Clan vs Clan Lobby OPEN! Type ^2!tyes ^7to represent your squad!"')

        elif command == "tstart":
            if len(msg_parts) > 1 and msg_parts[1] == "cancel":
                self.active_tournament = False
                self.send_rcon('say "^5[TOURNAMENT] ^1Tournament Cancelled by Admin."')
            else:
                self.lobby_open, self.is_cvc = True, False
                self.send_rcon('say "^5[TOURNAMENT] ^7Lobby OPEN! Type ^2!tyes ^7to join."')

        elif command == "tpause":
            self.tournament_paused = True
            self.send_rcon('say "^5[TOURNAMENT] ^1Global Pause Active."')

        elif command == "tresume":
            self.tournament_paused = False
            self.send_rcon('say "^5[TOURNAMENT] ^2Global Resume Active."')

        if len(msg_parts) < 2: return
        target_search = msg_parts[1].lower()
        p = next((x for x in self.players if target_search in x.clean_name), None)
        if not p: return

        if command == "clan" and len(msg_parts) >= 3:
            new_tag = msg_parts[2].upper()
            p.clan_tag = new_tag
            with sqlite3.connect(self.db_filename) as conn:
                conn.execute("UPDATE players SET clan_tag=? WHERE guid=?", (new_tag, p.guid))
            self.send_rcon(f'say "^5[ADMIN] ^7Set ^2{p.name}^7 clan to: ^3{new_tag}"')

        elif command == "group" and len(msg_parts) >= 3:
            new_group = msg_parts[2].upper()
            p.clan_group = new_group
            with sqlite3.connect(self.db_filename) as conn:
                conn.execute("UPDATE players SET clan_group=? WHERE guid=?", (new_group, p.guid))
            self.send_rcon(f'say "^5[ADMIN] ^7Assigned ^2{p.name} ^7to group: ^3{new_group}"')
        
        elif command == "promote":
            p.role = "OWNER"
            with sqlite3.connect(self.db_filename) as conn:
                conn.execute("UPDATE players SET clan_role='OWNER' WHERE guid=?", (p.guid,))
            self.send_rcon(f'say "^5[ADMIN] ^7Promoted ^2{p.name} ^7to ^5OWNER ^7of ^3{p.clan_tag}"')

        elif command == "resetplayer":
            with sqlite3.connect(self.db_filename) as conn:
                conn.execute("""UPDATE players SET duel_rating=1500, rating_deviation=350, 
                               total_rounds_won=0, tournament_wins=0 WHERE guid=?""", (p.guid,))
            p.rating, p.rd = 1500, 350
            self.send_rcon(f'say "^5[ADMIN] ^7Stats reset for ^2{p.name}^7."')

    def handle_chat(self, p, msg):
        cmd = msg.lower().split()
        if not cmd: return

        if cmd[0] == "!dpause" and p.opponent:
            p.is_paused = True
            self.send_rcon(f'svtell {p.id} "^5[DUEL] ^7Paused. Use !dresume when ready."')
            self.send_rcon(f'svtell {p.opponent.id} "^5[DUEL] ^2{p.name} ^7requested a pause."')

        elif cmd[0] == "!dresume" and p.opponent:
            p.is_paused = False
            self.send_rcon(f'say "^5[DUEL] ^2{p.name} ^7is ready to resume!"')
        
        if cmd[0] == "!dhelp":
            # Line 1: General & Duel
            line1 = "^5Duel: ^7!rank [name], !dtop, !fttop, !ttop, !dduel <name> <rounds>, !dforfeit, !dpause, !dresume"
            self.send_rcon(f'svtell {p.id} "{line1}"')
            
            # Line 2: Clan Management
            line2 = "^5Clan: ^7!dclantag register <tag>, !dclan show, !dclan join group <name>, !dclan quit"
            if p.role in ["OFFICER", "LEADER", "OWNER"]:
                line2 += " ^3Staff: ^7!tstart, !dclan promote/kick/rename/lock"
            self.send_rcon(f'svtell {p.id} "{line2}"')

        if cmd[0] == "!dclantag":
            if len(cmd) < 3: return
            sub, tag = cmd[1], cmd[2].upper()
            if sub == "register":
                with sqlite3.connect(self.db_filename) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM players WHERE clan_tag=?", (tag,))
                    role = "OWNER" if cursor.fetchone()[0] == 0 else "MEMBER"
                    conn.execute("UPDATE players SET clan_tag=?, clan_role=?, clan_group='DEFAULT' WHERE guid=?", (tag, role, p.guid))
                    conn.commit()
                p.clan_tag, p.role, p.clan_group = tag, role, "DEFAULT"
                self.send_rcon(f'svtell {p.id} "^5[CLAN] ^7Registered to ^3{tag} ^7as ^5{role}"')
            elif sub == "unregister":
                p.clan_tag, p.role, p.clan_group = "NONE", "MEMBER", "DEFAULT"
                with sqlite3.connect(self.db_filename) as conn:
                    conn.execute("UPDATE players SET clan_tag='NONE', clan_role='MEMBER', clan_group='DEFAULT' WHERE guid=?", (p.guid,))
                    conn.commit()
                self.send_rcon(f'svtell {p.id} "^5[CLAN] ^7Unregistered."')

        if cmd[0] == "!dclan" and len(cmd) >= 2:
            sub = cmd[1]
            if sub == "show":
                with sqlite3.connect(self.db_filename) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT name, clan_role, clan_group FROM players WHERE clan_tag=? AND clan_tag!='NONE'", (p.clan_tag,))
                    members = [f"{r[0]} ({r[1]} - {r[2]})" for r in cursor.fetchall()]
                    self.send_rcon(f'svtell {p.id} "^5[{p.clan_tag} ROSTER]: ^7{", ".join(members)}"')
            
            elif sub == "promote" and p.role in ["LEADER", "OWNER"]:
                if len(cmd) < 3: return
                target_search = cmd[2].lower()
                target_p = next((x for x in self.players if target_search in x.clean_name and x.clan_tag == p.clan_tag), None)
                if target_p:
                    new_role = "LEADER" if p.role == "OWNER" else "OFFICER"
                    target_p.role = new_role
                    with sqlite3.connect(self.db_filename) as conn:
                        conn.execute("UPDATE players SET clan_role=? WHERE guid=?", (new_role, target_p.guid))
                        conn.commit()
                    self.send_rcon(f'say "^5[CLAN] ^2{p.name} ^7promoted ^2{target_p.name} ^7to ^5{new_role}^7!"')

            elif sub == "rename" and p.role in ["LEADER", "OWNER"]:
                if len(cmd) < 4: return
                old_name, new_name = cmd[2].upper(), cmd[3].upper()
                with sqlite3.connect(self.db_filename) as conn:
                    conn.execute("UPDATE players SET clan_group=? WHERE clan_tag=? AND clan_group=?", (new_name, p.clan_tag, old_name))
                    conn.commit()
                for member in self.players:
                    if member.clan_tag == p.clan_tag and member.clan_group == old_name:
                        member.clan_group = new_name
                self.send_rcon(f'say "^5[CLAN] ^7Leader renamed division ^3{old_name} ^7to ^5{new_name}^7."')                

            elif sub == "group" and p.role in ["LEADER", "OWNER"]:
                if len(cmd) < 4: return
                target_search, group_name = cmd[2].lower(), cmd[3].upper()
                target_p = next((x for x in self.players if target_search in x.clean_name), None)
                if target_p and target_p.clan_tag == p.clan_tag:
                    target_p.clan_group = group_name
                    with sqlite3.connect(self.db_filename) as conn:
                        conn.execute("UPDATE players SET clan_group=? WHERE guid=?", (group_name, target_p.guid))
                        conn.commit()
                    self.send_rcon(f'say "^5[CLAN] ^2{target_p.name} ^7moved to subdivision: ^3{group_name}"')

            elif sub == "kick" and p.role in ["LEADER", "OWNER"]:
                target_name = cmd[2].lower()
                with sqlite3.connect(self.db_filename) as conn:
                    conn.execute("UPDATE players SET clan_tag='NONE', clan_role='MEMBER', clan_group='DEFAULT' WHERE clean_name=? AND clan_tag=?", (target_name, p.clan_tag))
                    conn.commit()
                self.send_rcon(f'say "^5[CLAN] ^2{p.name} ^7kicked ^1{target_name} ^7from clan."')

            elif sub == "quit":
                p.clan_tag, p.role, p.clan_group = "NONE", "MEMBER", "DEFAULT"
                with sqlite3.connect(self.db_filename) as conn:
                    conn.execute("UPDATE players SET clan_tag='NONE', clan_role='MEMBER', clan_group='DEFAULT' WHERE guid=?", (p.guid,))
                    conn.commit()
                self.send_rcon(f'say "^5[CLAN] ^2{p.name} ^7has left their clan."')

            elif sub == "lock" and p.role in ["LEADER", "OWNER"]:
                if len(cmd) < 3: return
                group_name = cmd[2].upper()
                clan_locks = self.locked_groups.get(p.clan_tag, [])
                if group_name in clan_locks:
                    clan_locks.remove(group_name)
                    self.send_rcon(f'say "^5[CLAN] ^3{group_name} ^7is now ^2OPEN^7."')
                else:
                    clan_locks.append(group_name)
                    self.send_rcon(f'say "^5[CLAN] ^3{group_name} ^7is now ^1LOCKED ^7(Invite Only)."')
                self.locked_groups[p.clan_tag] = clan_locks

            elif sub == "join" and len(cmd) >= 3 and cmd[2] == "group":
                group_name = cmd[3].upper()
                if p.clan_tag == "NONE": return
                clan_locks = self.locked_groups.get(p.clan_tag, [])
                if group_name in clan_locks:
                    p.pending_group_request = group_name
                    self.send_rcon(f'svtell {p.id} "^5[CLAN] ^7Request sent to join ^3{group_name}^7."')
                    for ldr in [x for x in self.players if x.clan_tag == p.clan_tag and x.role in ["LEADER", "OWNER"]]:
                        self.send_rcon(f'svtell {ldr.id} "^5[REQ] ^2{p.name} ^7wants to join ^3{group_name}^7. Type: ^2!daccept {p.id}"')
                else:
                    p.clan_group = group_name
                    with sqlite3.connect(self.db_filename) as conn:
                        conn.execute("UPDATE players SET clan_group=? WHERE guid=?", (group_name, p.guid))
                    self.send_rcon(f'svtell {p.id} "^5[CLAN] ^7Joined group ^3{group_name}"')

        elif cmd[0] == "!daccept" and p.role in ["LEADER", "OWNER"]:
            if len(cmd) < 2: return
            target_p = next((x for x in self.players if x.id == int(cmd[1])), None)
            if target_p and target_p.pending_group_request and target_p.clan_tag == p.clan_tag:
                group = target_p.pending_group_request
                target_p.clan_group = group
                target_p.pending_group_request = None
                with sqlite3.connect(self.db_filename) as conn:
                    conn.execute("UPDATE players SET clan_group=? WHERE guid=?", (group, target_p.guid))
                self.send_rcon(f'say "^5[CLAN] ^2{p.name} ^7approved ^2{target_p.name} ^7for ^3{group}^7!"')

        elif cmd[0] == "!ddecline" and p.role in ["LEADER", "OWNER"]:
            if len(cmd) < 2: return
            target_p = next((x for x in self.players if x.id == int(cmd[1])), None)
            if target_p:
                target_p.pending_group_request = None
                self.send_rcon(f'svtell {target_p.id} "^1[CLAN] ^7Your group request was declined."')

        elif cmd[0] == "!thelp":
            t_msg = "^5[TOURNAMENT]: ^7!tyes (Join Lobby), !tforfeit (Surrender Match), !thelp"
            
            # Additional commands visible only to staff (OFFICER and above)
            if p.role != "MEMBER":
                t_msg += " ^3Staff: ^7!tstart <score>, !tpause, !tresume"
                
            self.send_rcon(f'svtell {p.id} "{t_msg}"')

        elif cmd[0] == "!rank":
            # Determine if looking for self or another player
            target = p
            if len(cmd) > 1:
                target_search = " ".join(cmd[1:])
                found = next((x for x in self.players if target_search.lower() in x.clean_name), None)
                if found:
                    target = found

            # Fetch the data from the database
            with sqlite3.connect(self.db_filename) as conn:
                cursor = conn.cursor()
                cursor.execute("""SELECT duel_rating, total_rounds_won, tournament_wins 
                               FROM players WHERE guid = ?""", (target.guid,))
                data = cursor.fetchone()
                
                if data:
                    rating, rounds, t_wins = data
                    rank_msg = (f"^5Rank for ^2{target.name}: ^7Rating: ^3{int(rating)} ^7| "
                                 f"Rounds: ^3{rounds} ^7| Tourney Wins: ^3{t_wins}")
                    self.send_rcon(f'svtell {p.id} "{rank_msg}"')
                else:
                    self.send_rcon(f'svtell {p.id} "^1Error: ^7No rank data found for that player."')

        elif cmd[0] == "!tstart":
            # Hierarchy Check: MEMBER is index 0. We only want index 1 and above.
            if p.role == "MEMBER":
                return self.send_rcon(f'svtell {p.id} "^1Error: ^7Minimum rank [OFFICER] required to start tournaments."')
            
            # If they are NOT a MEMBER, proceed with starting the lobby
            self.lobby_open, self.lobby_players, self.is_cvc = True, [], False
            self.win_limit = int(cmd[1]) if len(cmd) > 1 and cmd[1].isdigit() else 5
            self.send_rcon(f'say "^5[TOURNAMENT] ^7Lobby OPEN! Type ^2!tyes ^7to join."')
            threading.Timer(60.0, self.start_tournament).start()

        elif cmd[0] == "!tyes" and self.lobby_open:
            if p not in self.lobby_players: self.lobby_players.append(p)

        elif cmd[0] == "!tforfeit" and self.active_tournament and p.opponent:
            winner = p.opponent
            
            # NEW: Clear persistent data so the tournament forfeit doesn't restore later
            with sqlite3.connect(self.db_filename) as conn:
                conn.execute("DELETE FROM active_matches WHERE (p1_guid=? AND p2_guid=?) OR (p1_guid=? AND p2_guid=?)",
                             (p.guid, winner.guid, winner.guid, p.guid))
                conn.commit()

            self.send_rcon(f'say "^5[FORFEIT] ^2{p.name} ^7surrendered to ^2{winner.name}^7."')
            self.finalize_match(winner, p)
        elif cmd[0] == "!dduel":
            # Usage: !dduel <name> <rounds>
            if len(cmd) < 3:
                return self.send_rcon(f'svtell {p.id} "^1Usage: ^7!dduel <name> <rounds>"')

            try:
                # The last argument is the rounds
                rounds = int(cmd[-1])
                # Everything between the command and the rounds is the name (handles spaces)
                target_search = " ".join(cmd[1:-1]).lower()
            except ValueError:
                return self.send_rcon(f'svtell {p.id} "^1Error: ^7Rounds must be a number. Example: !dduel Valzhar 10"')

            target = next((x for x in self.players if target_search in x.clean_name), None)
            
            if not target:
                return self.send_rcon(f'svtell {p.id} "^1Error: ^7Player \'{target_search}\' not found."')
                
            if target == p:
                return self.send_rcon(f'svtell {p.id} "^1Error: ^7You cannot duel yourself."')

            # Set challenge state
            target.pending_invite_from = p
            target.pending_limit = rounds
            
            self.send_rcon(f'say "^5[CHALLENGE] ^2{p.name} ^7challenged ^2{target.name} ^7to First to ^3{rounds}^7!"')
            self.send_rcon(f'svtell {target.id} "^7Type ^2!dyes ^7or ^1!dno ^7to respond."')

        elif cmd[0] == "!dyes" and p.pending_invite_from:
            inviter = p.pending_invite_from
            p.opponent, inviter.opponent = inviter, p
            p.match_score = inviter.match_score = 0
            self.win_limit = p.pending_limit 
            self.send_rcon(f'say "^5[DUEL] ^2{inviter.name} ^7vs ^2{p.name} ^7started (FT{p.pending_limit})!"')
            p.pending_invite_from = None

        elif cmd[0] == "!dno" and p.pending_invite_from:
            p.pending_invite_from = None

        elif cmd[0] == "!dforfeit" and p.opponent and not self.active_tournament:
            winner = p.opponent
            
            # NEW: Clear persistent data so the forfeited match doesn't restore next map
            with sqlite3.connect(self.db_filename) as conn:
                conn.execute("DELETE FROM active_matches WHERE (p1_guid=? AND p2_guid=?) OR (p1_guid=? AND p2_guid=?)",
                             (p.guid, winner.guid, winner.guid, p.guid))
                conn.commit()

            self.send_rcon(f'say "^5[DUEL] ^2{p.name} ^7forfeited. ^2{winner.name} ^7wins!"')
            p.opponent = winner.opponent = None

        elif cmd[0] == "!dtop":
            self.show_leaderboard("duel_rating", "Duel Ratings (Glicko-2)", p.id)
        elif cmd[0] == "!fttop":
            self.show_leaderboard("total_rounds_won", "Total Rounds Won", p.id)
        elif cmd[0] == "!ttop":
            self.show_leaderboard("tournament_wins", "Tournament Wins", p.id)
        elif cmd[0] == "!dclantop":
            self.show_clan_leaderboard(p.id)

    def show_leaderboard(self, column, label, sid):
        with sqlite3.connect(self.db_filename) as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT name, {column} FROM players WHERE guid != '0' ORDER BY {column} DESC LIMIT 5")
            rows = cursor.fetchall()
            self.send_rcon(f'svtell {sid} "^5--- TOP 5 {label} ---"')
            for i, (name, val) in enumerate(rows, 1):
                self.send_rcon(f'svtell {sid} "^7{i}. ^2{name} ^7- ^3{int(val)}"')

    def show_clan_leaderboard(self, sid):
        with sqlite3.connect(self.db_filename) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT clan_tag, AVG(duel_rating) as avg_r FROM players WHERE clan_tag != 'NONE' GROUP BY clan_tag ORDER BY avg_r DESC LIMIT 5")
            rows = cursor.fetchall()
            self.send_rcon(f'svtell {sid} "^5--- TOP 5 CLANS (Avg Rating) ---"')
            for i, (clan, avg) in enumerate(rows, 1):
                self.send_rcon(f'svtell {sid} "^7{i}. ^2{clan} ^7- ^3{int(avg)}"')

    def start_tournament(self):
        self.lobby_open = False
        if len(self.lobby_players) < 2:
            self.send_rcon('say "^5[TOURNAMENT] ^1Cancelled: Need at least 2 players."')
            return
        self.active_tournament = True
        self.setup_round(self.lobby_players)

    def setup_round(self, participants):
        self.current_round_matches = []
        self.round_winners = []
        for p in participants:
            p.match_score, p.opponent, p.is_paused = 0, None, False
        
        if self.is_cvc:
            temp_list = list(participants)
            while len(temp_list) > 1:
                p1 = temp_list.pop(0)
                opponent = next((x for x in temp_list if x.clan_tag != p1.clan_tag and x.clan_group != p1.clan_group), None)
                if not opponent:
                    opponent = next((x for x in temp_list if x.clan_tag != p1.clan_tag), temp_list[0])
                temp_list.remove(opponent)
                p1.opponent, opponent.opponent = opponent, p1
                self.current_round_matches.append((p1, opponent))
        else:
            participants.sort(key=lambda x: x.rating, reverse=True)
            for i in range(0, len(participants) - 1, 2):
                p1, p2 = participants[i], participants[i+1]
                p1.opponent, p2.opponent = p2, p1
                self.current_round_matches.append((p1, p2))
        
        if len(participants) % 2 != 0:
            self.round_winners.append(participants[-1])
        self.send_rcon(f'say "^5[ROUND {self.tournament_round_num}] ^7Matches STARTING."')

    def finalize_match(self, winner, loser):
        # NEW: Clear persistent data so it doesn't restore next map
        with sqlite3.connect(self.db_filename) as conn:
            conn.execute("DELETE FROM active_matches WHERE (p1_guid=? AND p2_guid=?) OR (p1_guid=? AND p2_guid=?)",
                         (winner.guid, loser.guid, loser.guid, winner.guid))
            conn.commit()

        if self.active_tournament:
            self.round_winners.append(winner)
            self.current_round_matches = [m for m in self.current_round_matches if winner not in m]
            winner.opponent = loser.opponent = None
            if not self.current_round_matches:
                if len(self.round_winners) > 1:
                    self.tournament_round_num += 1
                    self.setup_round(self.round_winners)
                else:
                    self.send_rcon(f'say "^5[CHAMPION] ^2{self.round_winners[0].name} ^7WON!"')
                    if self.round_winners[0].guid != "0":
                        with sqlite3.connect(self.db_filename) as conn:
                            conn.execute("UPDATE players SET tournament_wins = tournament_wins + 1 WHERE guid=?", (self.round_winners[0].guid,))
                            conn.commit()
                    self.active_tournament = False

    def run(self):
        log = self.settings['logname']
        last_sz = os.path.getsize(log) if os.path.exists(log) else 0
        while True:
            if not os.path.exists(log):
                time.sleep(1); continue
            curr_sz = os.path.getsize(log)
            if curr_sz < last_sz: last_sz = 0
            if curr_sz > last_sz:
                with open(log, 'r', encoding='utf-8', errors='ignore') as f:
                    f.seek(last_sz)
                    for line in f:
                        if "InitGame:" in line:
                            self.send_rcon('say "^5[SYSTEM] ^7Map/Round change detected. Restoring states..."')
                            # Clear temporary local lists but keep the database-backed players
                            self.lobby_players = []
                            self.active_tournament = False

                        elif "ClientUserinfoChanged:" in line:
                            m = re.search(r'ClientUserinfoChanged:\s*(\d+)\s*n\\([^\\]+)', line)
                            if m:
                                sid, name = int(m.group(1)), m.group(2)
                                # This ensures the player exists in memory before they even try to chat
                                self.sync_player(sid, name)                          
                        # ADMIN OPS
                        if "SMOD smsay:" in line:
                            smod_match = re.search(r'SMOD smsay:\s+(.*?)\s+\(adminID:\s+(\d+)\).*?\):\s*(.*)$', line)
                            if smod_match:
                                self.handle_smod_command(smod_match.group(1), smod_match.group(2), smod_match.group(3))

                        m_spawn = re.search(r'Player\s+(\d+).*?\\name\\(.*?)\\.*?ja_guid\\([A-Z0-9]{32})', line)
                        if m_spawn: self.sync_player(int(m_spawn.group(1)), m_spawn.group(2), m_spawn.group(3))
                        
                        # DUEL LOGIC
                        m_start = re.search(r'DuelStart: (.*?) challenged (.*?) to a private duel', line)
                        if m_start:
                            self.send_rcon(f'say "^5[DUEL] ^7Challenge detected: ^2{m_start.group(1)} ^7vs ^2{m_start.group(2)}"')

                        m_end = re.search(r'DuelEnd: (.*?) has defeated (.*?) in a private duel', line)
                        if m_end:
                            winner = next((p for p in self.players if p.clean_name == normalize(m_end.group(1)).lower()), None)
                            loser = next((p for p in self.players if p.clean_name == normalize(m_end.group(2)).lower()), None)
                            if winner and loser:
                                self.calculate_glicko2(winner, loser)
                                # Update Rating in DB
                                if winner.guid != "0":
                                    with sqlite3.connect(self.db_filename) as conn:
                                        conn.execute("UPDATE players SET duel_rating=?, rating_deviation=?, total_rounds_won = total_rounds_won + 1 WHERE guid=?", (winner.rating, winner.rd, winner.guid))
                                        conn.commit()
                                
                                # --- NEW PERSISTENCE CALL ---
                                if winner.opponent == loser:
                                    winner.match_score += 1
                                    self.save_match_progress(winner, loser) # Save score to DB immediately
                                    self.send_rcon(f'say "^5[MATCH] ^2{winner.name} ^7({winner.match_score}/{self.win_limit}) vs ^2{loser.name}"')
                                    if winner.match_score >= self.win_limit:
                                        self.finalize_match(winner, loser)

                        m_chat = re.search(r'(\d+):\s+say:\s+".*?"\s+"!(.*)"', line)
                        if m_chat:
                            p = next((x for x in self.players if x.id == int(m_chat.group(1))), None)
                            if p: self.handle_chat(p, "!" + m_chat.group(2))


                        if "say:" in line.lower():
                            try:
                                # 1. Extract mashed SID (e.g., "314: say:" -> 4)
                                log_sid = -1
                                sid_match = re.search(r'(\d+)[:\s]*say:', line, re.IGNORECASE)
                                if sid_match:
                                    sid_str = sid_match.group(1)
                                    # Skip first 2 digits if length > 2 (Time+ID), else use as-is
                                    log_sid = int(sid_str[2:]) if len(sid_str) > 2 else int(sid_str)

                                # 2. Extract Name and Message: say: Name: "Message"
                                msg_match = re.search(r'say:\s*(.*?):\s*"(.*)"', line)
                                if msg_match:
                                    raw_name = msg_match.group(1)
                                    message = msg_match.group(2).strip()
                                    # Normalize name for lookup
                                    clean_name_raw = re.sub(r'\^.', '', raw_name).strip().lower()

                                    # 3. Find Player: Try ID first, then fallback to Name matching
                                    p = next((x for x in self.players if x.id == log_sid), None)
                                    if not p:
                                        p = next((x for x in self.players if x.clean_name == clean_name_raw), None)

                                    if p:
                                        # Synchronize ID if it was mashed/shifted
                                        p.id = log_sid 
                                        self.handle_chat(p, message)
                                    else:
                                        # Final Fail-safe: If unknown, create temp player to allow commands
                                        p_temp = Player(log_sid, raw_name, "0")
                                        self.handle_chat(p_temp, message)
                            except Exception as e:
                                print(f"[PARSER ERROR] Say line failed: {e}")

                        elif "tell:" in line.lower():
                            try:
                                # 1. Extract SID from tell (e.g., "140 tell: ...")
                                sid_match = re.search(r'(\d+)\s*tell:', line, re.IGNORECASE)
                                if sid_match:
                                    sid_str = sid_match.group(1)
                                    log_sid = int(sid_str[2:]) if len(sid_str) > 2 else int(sid_str[-1])
                                    
                                    # 2. Extract Message
                                    msg_match = re.search(r'tell:.*?: "(.*)"', line)
                                    if msg_match:
                                        message = msg_match.group(1).strip()
                                        
                                        # 3. Find Player: ID then Name fallback
                                        p = next((x for x in self.players if x.id == log_sid), None)
                                        if not p:
                                            name_match = re.search(r'tell:\s*(.*?)\s+to', line)
                                            if name_match:
                                                raw_n = name_match.group(1).strip().lower()
                                                p = next((x for x in self.players if x.clean_name == raw_n), None)
                                        
                                        if p:
                                            self.handle_chat(p, message)
                            except Exception as e:
                                print(f"[PARSER ERROR] Tell line failed: {e}")
                                                
                last_sz = curr_sz
            time.sleep(0.2)

    def sync_player(self, sid, name, guid):
        valid_guid = guid and guid != "0" and len(guid) > 10
        clan = self.detect_clan(name)
        current_name = normalize(name)
        current_clean = current_name.lower()
        rating, rd, role, group = 1500, 350, "MEMBER", "DEFAULT"
        
        if valid_guid:
            with sqlite3.connect(self.db_filename) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name, duel_rating, rating_deviation, clan_tag, clan_role, clan_group FROM players WHERE guid = ?", (guid,))
                data = cursor.fetchone()
                if data: 
                    db_name, rating, rd, db_clan, role, group = data
                    if db_name != current_name:
                        conn.execute("UPDATE players SET name=?, clean_name=? WHERE guid=?", (current_name, current_clean, guid))
                    clan = db_clan if db_clan != "NONE" else clan
                else: 
                    conn.execute("INSERT INTO players (guid, name, clean_name, clan_tag) VALUES (?, ?, ?, ?)", (guid, current_name, current_clean, clan))
                
                # RESTORE MATCH PROGRESS
                cursor.execute("SELECT p2_guid, p1_score, p2_score, win_limit FROM active_matches WHERE p1_guid=?", (guid,))
                match_data = cursor.fetchone()
                if match_data:
                    opp_guid, p1_score, p2_score, w_limit = match_data
                    # Check if the opponent is already on the server
                    opponent = next((x for x in self.players if x.guid == opp_guid), None)
                    if opponent:
                        # Re-establish the duel link
                        self.win_limit = w_limit
                        new_p = Player(sid, name, guid, rating, rd, clan=clan, role=role, group=group)
                        new_p.match_score, opponent.match_score = p1_score, p2_score
                        new_p.opponent, opponent.opponent = opponent, new_p
                        self.send_rcon(f'say "^5[RESUME] ^7Duel restored: ^2{new_p.name} ^7({p1_score}) vs ^2{opponent.name} ^7({p2_score})"!')
                conn.commit()

        self.players = [p for p in self.players if p.id != sid]
        self.players.append(Player(sid, name, guid, rating, rd, clan=clan, role=role, group=group))

    def send_rcon(self, command):
        try:
            client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            client.settimeout(1.0)
            packet = b'\xff\xff\xff\xff' + f'rcon "{self.settings["rcon"]}" {command}'.encode()
            client.sendto(packet, (self.settings["ip"], int(self.settings["port"])))
            client.close()
        except Exception as e:
            print(f"RCON Error: {e}")

if __name__ == "__main__":

    while True:
        try:
            # Initialize and run the plugin
            plugin = MBIIDuelPlugin()
            plugin.run()
            
        except KeyboardInterrupt:
            print("\n[SYSTEM] Manual shutdown. Performing final save...")
            # We don't need a massive loop here because ratings are saved 
            # mid-match, but we ensure the DB connection is closed.
            sys.exit(0)
            
        except Exception as e:
            # This catches any unexpected code crashes
            print(f"!!! CRASH DETECTED: {e}")
            print("Attempting emergency safety save...")
            
            try:
                # In your duel script, we want to make sure current ratings 
                # for all active players are flushed to the DB.
                with sqlite3.connect(plugin.db_filename) as conn:
                    for p in plugin.players:
                        if p.guid != "0":
                            conn.execute("""UPDATE players SET duel_rating=?, 
                                         rating_deviation=? WHERE guid=?""", 
                                         (p.rating, p.rd, p.guid))
                    conn.commit()
                print("Emergency rating save successful.")
            except Exception as save_error:
                print(f"Emergency save failed: {save_error}")
            
            print("Restarting plugin in 5 seconds...")
            time.sleep(5)