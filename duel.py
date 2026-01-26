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
        self.role = role  # MEMBER or LEADER
        self.clan_group = group # Subdivision/Squad
        # Tournament/Match Variables
        self.match_score = 0
        self.opponent = None
        self.is_paused = False
        # Challenge Logic
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
        self.current_round_matches = [] 
        self.round_winners = []
        self.win_limit = 5
        self.tournament_round_num = 1
        self.locked_groups = {}

    def init_sqlite(self):
        with sqlite3.connect(self.db_filename, timeout=20) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS players (
                    guid TEXT PRIMARY KEY,
                    name TEXT,
                    clean_name TEXT,
                    clan_tag TEXT DEFAULT 'NONE',
                    clan_role TEXT DEFAULT 'MEMBER',
                    clan_group TEXT DEFAULT 'DEFAULT',
                    duel_rating REAL DEFAULT 1500,
                    rating_deviation REAL DEFAULT 350,
                    total_rounds_won INTEGER DEFAULT 0,
                    tournament_wins INTEGER DEFAULT 0
                )
            ''')
            conn.commit()

    def load_config(self):
        config = configparser.ConfigParser()
        config.read(self.config_file)
        self.settings = dict(config['SETTINGS'])

    def detect_clan(self, name):
        """Auto Clan Tag Detection Logic."""
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
        """Processes SMOD commands for admins."""
        msg_parts = full_message.split()
        if not msg_parts: return

        command = msg_parts[0].lower().replace("!", "")

        # Commands below require a target player
        if len(msg_parts) < 2: return
        target_search = msg_parts[1].lower()
        p = next((x for x in self.players if target_search in x.clean_name), None)
        if not p: return

        # --- ADMIN CLAN OVERRIDE ---
        if command == "clan" and len(msg_parts) >= 3:
            new_tag = msg_parts[2].upper()
            p.clan_tag = new_tag
            with sqlite3.connect(self.db_filename) as conn:
                conn.execute("UPDATE players SET clan_tag=? WHERE guid=?", (new_tag, p.guid))
            self.send_rcon(f'say "^5[ADMIN] ^7Set ^2{p.name}^7 clan to: ^3{new_tag}"')
        
        # --- ADMIN PROMOTE ---
        elif command == "promote":
            p.role = "LEADER"
            with sqlite3.connect(self.db_filename) as conn:
                conn.execute("UPDATE players SET clan_role='LEADER' WHERE guid=?", (p.guid,))
            self.send_rcon(f'say "^5[ADMIN] ^7Promoted ^2{p.name} ^7to ^5LEADER ^7of ^3{p.clan_tag}"')

        # --- ADMIN RESET ---
        elif command == "resetplayer":
            with sqlite3.connect(self.db_filename) as conn:
                conn.execute("""UPDATE players SET duel_rating=1500, rating_deviation=350, 
                               total_rounds_won=0, tournament_wins=0 WHERE guid=?""", (p.guid,))
            p.rating, p.rd = 1500, 350
            self.send_rcon(f'say "^5[ADMIN] ^7Stats reset for ^2{p.name}^7."')

        elif command == "group" and len(msg_parts) >= 3:
            group_name = msg_parts[2].upper()
            p.clan_group = group_name
            with sqlite3.connect(self.db_filename) as conn:
                conn.execute("UPDATE players SET clan_group=? WHERE guid=?", (group_name, p.guid))
            
            action = "moved to" if group_name != "none" else "removed from"
            self.send_rcon(f'say "^5[ADMIN] ^7Force {action} group: ^3{group_name} ^7for ^2{p.name}"')    

    def handle_chat(self, p, msg):
        cmd = msg.lower().split()
        if not cmd: return
        
        # --- CLAN SYSTEM ---
        if cmd[0] == "!dclantag":
            if len(cmd) < 3: return
            sub, tag = cmd[1], cmd[2].upper()
            if sub == "register":
                with sqlite3.connect(self.db_filename) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM players WHERE clan_tag=?", (tag,))
                    role = "LEADER" if cursor.fetchone()[0] == 0 else "MEMBER"
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

        elif cmd[0] == "!dclan" and len(cmd) >= 2:
            sub = cmd[1]
            if sub == "show":
                with sqlite3.connect(self.db_filename) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT name, clan_role, clan_group FROM players WHERE clan_tag=? AND clan_tag!='NONE'", (p.clan_tag,))
                    members = [f"{r[0]} ({r[1]} - {r[2]})" for r in cursor.fetchall()]
                    self.send_rcon(f'svtell {p.id} "^5[{p.clan_tag} ROSTER]: ^7{", ".join(members)}"')
            
            elif sub == "promote" and p.role == "LEADER":
                if len(cmd) < 3: return
                target_search = cmd[2].lower()
                target_p = next((x for x in self.players if target_search in x.clean_name and x.clan_tag == p.clan_tag), None)
                if target_p:
                    target_p.role = "LEADER"
                    with sqlite3.connect(self.db_filename) as conn:
                        conn.execute("UPDATE players SET clan_role='LEADER' WHERE guid=?", (target_p.guid,))
                        conn.commit()
                    self.send_rcon(f'say "^5[CLAN] ^2{p.name} ^7promoted ^2{target_p.name} ^7to ^5LEADER^7!"')
                else:
                    self.send_rcon(f'svtell {p.id} "^1Error: ^7Player not found in your clan."')

            elif sub == "rename" and p.role == "LEADER":
                if len(cmd) < 4: return
                old_name, new_name = cmd[2].upper(), cmd[3].upper()
                
                with sqlite3.connect(self.db_filename) as conn:
                    # Update everyone in the DB with this tag and group
                    conn.execute("UPDATE players SET clan_group=? WHERE clan_tag=? AND clan_group=?", 
                                (new_name, p.clan_tag, old_name))
                    conn.commit()
                
                # Update currently online players locally
                for member in self.players:
                    if member.clan_tag == p.clan_tag and member.clan_group == old_name:
                        member.clan_group = new_name
                
                self.send_rcon(f'say "^5[CLAN] ^7Leader renamed division ^3{old_name} ^7to ^5{new_name}^7."')                

            # --- LEADER COMMAND: Move a member to a division ---
            elif sub == "group" and p.role == "LEADER":
                if len(cmd) < 4: return
                target_search, group_name = cmd[2].lower(), cmd[3].upper()
                target_p = next((x for x in self.players if target_search in x.clean_name), None)
                
                # Check if player exists AND has the same clan tag
                if target_p and target_p.clan_tag == p.clan_tag:
                    target_p.clan_group = group_name
                    with sqlite3.connect(self.db_filename) as conn:
                        conn.execute("UPDATE players SET clan_group=? WHERE guid=?", (group_name, target_p.guid))
                        conn.commit()
                    self.send_rcon(f'say "^5[CLAN] ^2{target_p.name} ^7moved to subdivision: ^3{group_name}"')
                else:
                    self.send_rcon(f'svtell {p.id} "^1Error: ^7Player not found or not in your clan."') 

            elif sub == "kick" and p.role == "LEADER":
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

            # NEW: Leader Lock Command
            elif sub == "lock" and p.role == "LEADER":
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

            # UPDATED: Join Logic
            elif sub == "join" and len(cmd) >= 3 and cmd[2] == "group":
                group_name = cmd[3].upper()
                if p.clan_tag == "NONE": return
                
                clan_locks = self.locked_groups.get(p.clan_tag, [])
                if group_name in clan_locks:
                    p.pending_group_request = group_name
                    self.send_rcon(f'svtell {p.id} "^5[CLAN] ^7Request sent to join ^3{group_name}^7."')
                    # Notify online leaders
                    for ldr in [x for x in self.players if x.clan_tag == p.clan_tag and x.role == "LEADER"]:
                        self.send_rcon(f'svtell {ldr.id} "^5[REQ] ^2{p.name} ^7wants to join ^3{group_name}^7. Type: ^2!daccept {p.id}"')
                else:
                    p.clan_group = group_name
                    with sqlite3.connect(self.db_filename) as conn:
                        conn.execute("UPDATE players SET clan_group=? WHERE guid=?", (group_name, p.guid))
                    self.send_rcon(f'svtell {p.id} "^5[CLAN] ^7Joined group ^3{group_name}"')

        # NEW: Global Leader Response Commands
        elif cmd[0] == "!daccept" and p.role == "LEADER":
            if len(cmd) < 2: return
            target_p = next((x for x in self.players if x.id == int(cmd[1])), None)
            if target_p and target_p.pending_group_request and target_p.clan_tag == p.clan_tag:
                group = target_p.pending_group_request
                target_p.clan_group = group
                target_p.pending_group_request = None
                with sqlite3.connect(self.db_filename) as conn:
                    conn.execute("UPDATE players SET clan_group=? WHERE guid=?", (group, target_p.guid))
                self.send_rcon(f'say "^5[CLAN] ^2{p.name} ^7approved ^2{target_p.name} ^7for ^3{group}^7!"')

        elif cmd[0] == "!ddecline" and p.role == "LEADER":
            if len(cmd) < 2: return
            target_p = next((x for x in self.players if x.id == int(cmd[1])), None)
            if target_p:
                target_p.pending_group_request = None
                self.send_rcon(f'svtell {target_p.id} "^1[CLAN] ^7Your group request was declined."')    

        # --- TOURNAMENT / CHALLENGES / LEADERBOARDS (Existing Logic) ---
        elif cmd[0] == "!tstart":
            self.lobby_open, self.lobby_players = True, []
            self.win_limit = int(cmd[1]) if len(cmd) > 1 else 5
            self.send_rcon(f'say "^5[TOURNAMENT] ^7Lobby OPEN! Type ^2!tyes ^7to join."')
            threading.Timer(60.0, self.start_tournament).start()
        elif cmd[0] == "!tyes" and self.lobby_open:
            if p not in self.lobby_players: self.lobby_players.append(p)
        elif cmd[0] == "!tforfeit" and self.active_tournament and p.opponent:
            winner = p.opponent
            self.send_rcon(f'say "^5[FORFEIT] ^2{p.name} ^7surrendered to ^2{winner.name}^7."')
            self.finalize_match(winner, p)

        elif cmd[0] == "!dduel":
            if len(cmd) < 2: return
            rounds = int(cmd[-1]) if cmd[-1].isdigit() else 5
            target_search = " ".join(cmd[1:-1]) if cmd[-1].isdigit() else " ".join(cmd[1:])
            target = next((x for x in self.players if target_search.lower() in x.clean_name), None)
            if target and target != p:
                target.pending_invite_from = p
                target.pending_limit = rounds
                self.send_rcon(f'say "^5[CHALLENGE] ^2{p.name} ^7challenged ^2{target.name} ^7to First to ^3{rounds}^7!"')
                self.send_rcon(f'svtell {target.id} "^7Type ^2!dyes ^7or ^1!dno"')
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
        participants.sort(key=lambda x: x.rating, reverse=True)
        for i in range(0, len(participants) - 1, 2):
            p1, p2 = participants[i], participants[i+1]
            p1.opponent, p2.opponent = p2, p1
            self.current_round_matches.append((p1, p2))
        if len(participants) % 2 != 0:
            self.round_winners.append(participants[-1])
        self.send_rcon(f'say "^5[ROUND {self.tournament_round_num}] ^7Matches STARTING."')

    def finalize_match(self, winner, loser):
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
                        if "SMOD smsay:" in line:
                            smod_match = re.search(r'SMOD smsay:\s+(.*?)\s+\(adminID:\s+(\d+)\).*?\):\s*(.*)$', line)
                            if smod_match:
                                self.handle_smod_command(smod_match.group(1), smod_match.group(2), smod_match.group(3))

                        m_spawn = re.search(r'Player\s+(\d+).*?\\name\\(.*?)\\.*?ja_guid\\([A-Z0-9]{32})', line)
                        if m_spawn: self.sync_player(int(m_spawn.group(1)), m_spawn.group(2), m_spawn.group(3))
                        
                        m_end = re.search(r'DuelEnd: (.*?) has defeated (.*?) in a private duel', line)
                        if m_end:
                            winner = next((p for p in self.players if p.clean_name == normalize(m_end.group(1)).lower()), None)
                            loser = next((p for p in self.players if p.clean_name == normalize(m_end.group(2)).lower()), None)
                            if winner and loser:
                                self.calculate_glicko2(winner, loser)
                                if winner.guid != "0":
                                    with sqlite3.connect(self.db_filename) as conn:
                                        conn.execute("UPDATE players SET duel_rating=?, rating_deviation=?, total_rounds_won = total_rounds_won + 1 WHERE guid=?", (winner.rating, winner.rd, winner.guid))
                                        conn.commit()
                                if winner.opponent == loser:
                                    winner.match_score += 1
                                    self.send_rcon(f'say "^5[MATCH] ^2{winner.name} ^7({winner.match_score}/{self.win_limit}) vs ^2{loser.name}"')
                                    if winner.match_score >= self.win_limit:
                                        self.finalize_match(winner, loser)

                        m_start = re.search(r'DuelStart: (.*?) challenged (.*?) to a private duel', line)
                        if m_start:
                            self.send_rcon(f'say "^5[DUEL] ^7Challenge detected: ^2{m_start.group(1)} ^7vs ^2{m_start.group(2)}"')          
                        
                        m_chat = re.search(r'(\d+):\s+say:\s+".*?"\s+"!(.*)"', line)
                        if m_chat:
                            p = next((x for x in self.players if x.id == int(m_chat.group(1))), None)
                            if p: self.handle_chat(p, "!" + m_chat.group(2))
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
                    
                    # Update name in DB if it changed on the server
                    if db_name != current_name:
                        conn.execute("UPDATE players SET name=?, clean_name=? WHERE guid=?", 
                                    (current_name, current_clean, guid))
                    
                    # Keep the manually set or registered clan tag
                    clan = db_clan if db_clan != "NONE" else clan
                else: 
                    # New player record
                    conn.execute("INSERT INTO players (guid, name, clean_name, clan_tag) VALUES (?, ?, ?, ?)", 
                                (guid, current_name, current_clean, clan))
                conn.commit()
        
        # Update local player list
        self.players = [p for p in self.players if p.id != sid]
        self.players.append(Player(sid, name, guid, rating, rd, clan=clan, role=role, group=group))

    def send_rcon(self, command):
        try:
            client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            client.settimeout(1.0) # Prevents the script from hanging on a bad connection
            packet = b'\xff\xff\xff\xff' + f'rcon "{self.settings["rcon"]}" {command}'.encode()
            client.sendto(packet, (self.settings["ip"], int(self.settings["port"])))
            client.close() # Good practice to close the socket after sending
        except Exception as e:
            print(f"RCON Error: {e}")

if __name__ == "__main__":
    MBIIDuelPlugin().run()