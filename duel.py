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
    # Remove colors (^1, ^2, etc)
    clean = re.sub(r'\^.', '', text)
    # Lowercase, strip spaces, and remove brackets to match the Player class
    return clean.lower().strip().replace('[', '').replace(']', '') 

class Player:
    def __init__(self, sid, name, guid, rating=1500, rd=350, vol=0.06, clan="NONE", role="MEMBER", group="DEFAULT"):
        self.id = sid
        self.name = name
        self.clean_name = re.sub(r'\^.', '', name).lower().strip().replace('[', '').replace(']', '')
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
        self.active_duels = set()
        self.start_time = time.time()
        self.slot_map = {}

        
        # Load any existing progress from previous map/round
        self.restore_match_progress()

    def force_sync_players(self):
        print("[DEBUG] Empty memory detected. Fetching server status...")
        # Get the 'status' from the server via RCON
        status_data = self.send_rcon("status")
        if not status_data:
            return

        # Typical line: "0 12345678 0 Benjin 0 127.0.0.1:29070"
        for line in status_data.splitlines():
            # This regex finds the Slot ID and Name from 'status'
            m = re.search(r'^\s*(\d+)\s+\d+\s+\d+\s+(.*?)\s+\d+\s+', line)
            if m:
                sid = int(m.group(1))
                name = m.group(2).strip()
                # Use your existing sync_player to put them in the DB and memory
                self.sync_player(sid, name, "0") 
        print(f"[DEBUG] Sync complete. Players in memory: {len(self.players)}")    

    def start_status_loop(self):
        def loop():
            while True:
                # Request status from server
                self.send_rcon("status")
                # Wait 60 seconds before asking again
                time.sleep(60)
                
        threading.Thread(target=loop, daemon=True).start()  
    

    def parse_status_line(self, line):
        # Example line: "0 12345 Valzhar 0 139.216.5.109:29070"
        # Logic depends on your specific game engine (e.g., Quake 3 / IW / Source)
        parts = line.split()
        try:
            if len(parts) >= 4 and parts[0].isdigit():
                slot_id = parts[0]
                player_name = parts[3] # Index varies by game
                
                # Create/Update player object in your list
                # This ensures handle_smod_command can find them!
                self.update_player_list(slot_id, player_name)
        except:
            pass 

    def update_player_slot(self, slot_id, name):
        clean_name = normalize(name).lower().replace('[', '').replace(']', '').strip()
        
        # Calculate the actual ID (0-31)
        try:
            raw_val = int(slot_id)
            actual_id = int(str(raw_val)[2:]) if raw_val > 32 else raw_val
        except:
            return -1

        # Find the player in your list
        player_obj = next((p for p in self.players if p.clean_name == clean_name), None)
        
        if player_obj:
            player_obj.id = actual_id
            self.slot_map[actual_id] = player_obj # SAVE TO SWITCHBOARD
            return actual_id
            
        return actual_id

    def init_sqlite(self):
        with sqlite3.connect(self.db_filename, timeout=20) as conn:
            cursor = conn.cursor()
            # 1. Create the players table with all necessary columns
            cursor.execute('''CREATE TABLE IF NOT EXISTS players (
                    guid TEXT PRIMARY KEY, 
                    name TEXT, 
                    clean_name TEXT, 
                    clan_tag TEXT DEFAULT 'NONE',
                    clan_role TEXT DEFAULT 'MEMBER', 
                    clan_group TEXT DEFAULT 'DEFAULT',
                    duel_rating REAL DEFAULT 1500, 
                    rating_deviation REAL DEFAULT 350,
                    total_rounds_won INTEGER DEFAULT 0, 
                    total_rounds_lost INTEGER DEFAULT 0,
                    tournament_wins INTEGER DEFAULT 0)''')
            
            # 2. Index clean_name to make Slot 0 lookups instant
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_clean_name ON players(clean_name)')
            
            # 3. Create active_matches for persistence
            cursor.execute('''CREATE TABLE IF NOT EXISTS active_matches (
                    p1_guid TEXT, 
                    p2_guid TEXT, 
                    p1_score INTEGER, 
                    p2_score INTEGER, 
                    win_limit INTEGER, 
                    is_cvc INTEGER)''')
            
            conn.commit()

        # 4. Migration Block: Add columns to existing databases that might be missing them
        with sqlite3.connect(self.db_filename) as conn:
            columns_to_add = [
                ("total_rounds_lost", "INTEGER DEFAULT 0"),
                # You can add more columns here in the future if needed
            ]
            for col_name, col_type in columns_to_add:
                try:
                    conn.execute(f"ALTER TABLE players ADD COLUMN {col_name} {col_type}")
                    conn.commit()
                    print(f"[SYSTEM] Database Migration: Added {col_name} column.")
                except sqlite3.OperationalError:
                    # Column already exists
                    pass 
                    
        with sqlite3.connect(self.db_filename) as conn:
            # This deletes any "junk" 1500 entries if a higher rating entry exists for the same name
            conn.execute("""
                DELETE FROM players 
                WHERE duel_rating = 1500 
                AND clean_name IN (
                    SELECT clean_name FROM players WHERE duel_rating > 1500
                )
            """)
            conn.commit()
            print("[SYSTEM] Cleaned up duplicate 1500-rating entries.")             

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
        try:
            # Glicko-2 Constants
            def g(rd): return 1 / math.sqrt(1 + 3 * (rd**2) / (math.pi**2))
            def E(r1, r2, rd2): return 1 / (1 + math.exp(-g(rd2) * (r1 - r2) / 173.7178))
            
            r1, rd1 = (winner.rating - 1500) / 173.7178, winner.rd / 173.7178
            r2, rd2 = (loser.rating - 1500) / 173.7178, loser.rd / 173.7178
            
            v1 = 1 / (g(rd2)**2 * E(r1, r2, rd2) * (1 - E(r1, r2, rd2)))
            new_rd1 = 1 / math.sqrt(1 / rd1**2 + 1 / v1)
            new_r1 = r1 + new_rd1**2 * (g(rd2) * (1 - E(r1, r2, rd2)))
            
            v2 = 1 / (g(rd1)**2 * E(r2, r1, rd1) * (1 - E(r2, r1, rd1)))
            new_rd2 = 1 / math.sqrt(1 / rd2**2 + 1 / v2)
            new_r2 = r2 + new_rd2**2 * (g(rd1) * (0 - E(r2, r1, rd1)))

            winner.rating, winner.rd = 1500 + 173.7178 * new_r1, max(30, 173.7178 * new_rd1)
            loser.rating, loser.rd = 1500 + 173.7178 * new_r2, max(30, 173.7178 * new_rd2)

            with sqlite3.connect(self.db_filename) as conn:
                # Winner Save
                w_valid = winner.guid and winner.guid != "0" and len(winner.guid) > 10
                conn.execute(f"UPDATE players SET duel_rating=?, rating_deviation=? WHERE {'guid' if w_valid else 'clean_name'}=?", 
                             (winner.rating, winner.rd, winner.guid if w_valid else winner.clean_name))
                
                # Loser Save
                l_valid = loser.guid and loser.guid != "0" and len(loser.guid) > 10
                conn.execute(f"UPDATE players SET duel_rating=?, rating_deviation=? WHERE {'guid' if l_valid else 'clean_name'}=?", 
                             (loser.rating, loser.rd, loser.guid if l_valid else loser.clean_name))
                conn.commit()

        except Exception as e:
            print(f"[DB ERROR] Glicko Update Failed: {e}")

    def handle_smod_command(self, raw_admin_name, admin_id, full_message):
        """Processes SMOD commands and translates SMOD ID 1-32 to Game Slot 0-31."""
        try:
            # 1. Sync the Admin and get the corrected Game Slot (ID - 1)
            active_slot = self.update_player_slot(admin_id, raw_admin_name)

            msg_parts = full_message.split()
            if not msg_parts:
                return

            # Extract command and strip '!'
            command = msg_parts[0].lower().lstrip("!")
            
            # Clean name for matching and display
            admin_display = normalize(raw_admin_name)
            admin_pure = admin_display.lower().replace('[', '').replace(']', '').strip()

            # 2. HELP / FEEDBACK COMMANDS
            if command in ["dhelp", "help"]:
                # This will now send 'svtell 0' if SMOD reported ID 1
                self.send_rcon(f'svtell {active_slot} "^5[ADMIN] ^7Commands: !clan, !group, !promote, !resetplayer, !cstart, !tstart, !tpause, !tresume"')
                # print(f"[DEBUG] Admin: {admin_pure} | SMOD ID: {admin_id} -> Mapped to Game Slot: {active_slot}")
                return

            # 3. LOBBY CONTROLS
            if command == "cstart":
                if len(msg_parts) > 1 and msg_parts[1] == "cancel":
                    self.active_tournament = self.is_cvc = False
                    self.send_rcon('say "^5[CvC] ^1Clan Match Cancelled by Admin."')
                else:
                    self.lobby_open, self.is_cvc = True, True
                    self.send_rcon('say "^5[CvC] ^7Clan vs Clan Lobby OPEN! Type ^2!tyes ^7to represent your squad!"')
                return

            if command == "tstart":
                if len(msg_parts) > 1 and msg_parts[1] == "cancel":
                    self.active_tournament = False
                    self.send_rcon('say "^5[TOURNAMENT] ^1Tournament Cancelled by Admin."')
                else:
                    self.lobby_open, self.is_cvc = True, False
                    self.send_rcon('say "^5[TOURNAMENT] ^7Lobby OPEN! Type ^2!tyes ^7to join."')
                return

                        # --- ADMIN CLAN LOOKUP ---
            if command == "clanlist":
                with sqlite3.connect(self.db_filename) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT DISTINCT clan_tag FROM players WHERE clan_tag != 'NONE'")
                    clans = cursor.fetchall()
                    
                if not clans:
                    self.send_rcon(f'svtell {active_slot} "^1No clans found in database."')
                else:
                    self.send_rcon(f'svtell {active_slot} "^5--- ALL REGISTERED CLANS ---"')
                    for i, (tag,) in enumerate(clans, 1):
                        self.send_rcon(f'svtell {active_slot} "^7{i}. ^3{tag}"')
                return  

            # --- ADMIN CLAN DELETE ---
            elif command == "clandelete" and len(msg_parts) >= 2:
                target_tag = msg_parts[1].upper()
                
                with sqlite3.connect(self.db_filename) as conn:
                    cursor = conn.cursor()
                    
                    # 1. Check if the clan actually exists in the database
                    cursor.execute("SELECT COUNT(*) FROM players WHERE clan_tag = ? AND clan_tag != 'NONE'", (target_tag,))
                    exists = cursor.fetchone()[0]
                    
                    if exists == 0:
                        # Clan does not exist
                        self.send_rcon(f'svtell {active_slot} "^1Error: ^7Clan ^3{target_tag} ^7does not exist in the database."')
                        return # Exit early

                    # 2. If it exists, proceed with the deletion
                    conn.execute("UPDATE players SET clan_tag='NONE', clan_role='MEMBER', clan_group='DEFAULT' WHERE clan_tag=?", (target_tag,))
                    conn.commit()
                
                # 3. Update live memory for any players currently online
                for p_obj in self.players:
                    if p_obj.clan_tag == target_tag:
                        p_obj.clan_tag, p_obj.role, p_obj.clan_group = "NONE", "MEMBER", "DEFAULT"
                
                self.send_rcon(f'say "^5[ADMIN] ^7Clan ^3{target_tag} ^7has been successfully disbanded."')
                return     

            # 4. TARGET PLAYER LOOKUP (For !clan, !promote, etc.)
            if len(msg_parts) < 2:
                return

            target_search = msg_parts[1].lower()
            target_p = None
            
            # Search the known players list for the target
            for x in self.players:
                try:
                    p_name_clean = normalize(x.name).lower().replace('[', '').replace(']', '').strip()
                    if target_search in p_name_clean:
                        target_p = x
                        break
                except:
                    continue
            
            if not target_p:
                self.send_rcon(f'svtell {active_slot} "^1Error: ^7Player \'{target_search}\' not found."')
                return

            # 5. DATABASE ACTIONS
            action_text = ""

            if command == "clan" and len(msg_parts) >= 3:
                new_tag = msg_parts[2].upper()
                target_p.clan_tag = new_tag
                with sqlite3.connect(self.db_filename) as conn:
                    conn.execute("UPDATE players SET clan_tag=? WHERE guid=?", (new_tag, target_p.guid))
                action_text = f"^7set ^5{target_p.name}^7 clan to: ^5{new_tag}"

            elif command == "group" and len(msg_parts) >= 3:
                new_group = msg_parts[2].upper()
                target_p.clan_group = new_group
                with sqlite3.connect(self.db_filename) as conn:
                    conn.execute("UPDATE players SET clan_group=? WHERE guid=?", (new_group, target_p.guid))
                action_text = f"^7assigned ^5{target_p.name} ^7to group: ^5{new_group}"

            elif command == "promote":
                with sqlite3.connect(self.db_filename) as conn:
                    conn.execute("UPDATE players SET clan_role='OWNER' WHERE guid=?", (target_p.guid,))
                action_text = f"^7promoted ^5{target_p.name} ^7to ^5OWNER"

            elif command == "resetplayer":
                with sqlite3.connect(self.db_filename) as conn:
                    conn.execute("UPDATE players SET duel_rating=1500, rating_deviation=350 WHERE guid=?", (target_p.guid,))
                action_text = f"^7reset stats for ^5{target_p.name}"  

            # 6. BROADCAST SUCCESS
            if action_text:
                self.send_rcon(f'say "^5[ADMIN] ^7{admin_display} {action_text}"')

        except Exception as e:
            # This catch-all will now tell us EXACTLY what variable is missing if it fails again
            print(f"[ERROR] handle_smod_command failed: {e}")

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
            if len(cmd) < 3:
                self.send_rcon(f'svtell {p.id} "^1Usage: ^7!dclantag register <TAG>"')
                return
                
            sub_cmd = cmd[1].lower()
            new_tag = cmd[2].upper().strip()

            if sub_cmd == "register":
                # 1. Check if the player is already in a clan
                if p.clan_tag != "NONE":
                    self.send_rcon(f'svtell {p.id} "^1Error: ^7You are already in clan ^5{p.clan_tag}^7. You must leave it first."')
                    return

                # 2. Check if the clan tag they want to join already exists
                with sqlite3.connect(self.db_filename) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT name FROM players WHERE clan_tag=? AND clan_role='OWNER' LIMIT 1", (new_tag,))
                    owner_data = cursor.fetchone()

                    if owner_data:
                        # Clan exists - Join as a MEMBER
                        role = "MEMBER"
                        msg = f"^5[CLAN] ^7Joined existing clan ^3{new_tag} ^7as ^5MEMBER."
                    else:
                        # Clan is brand new - Join as OWNER
                        role = "OWNER"
                        msg = f"^5[CLAN] ^7Clan ^3{new_tag} ^7created. You are the ^5OWNER."

                    # 3. Save to Database and update live Player object
                    conn.execute("UPDATE players SET clan_tag=?, clan_role=?, clan_group='DEFAULT' WHERE guid=?", 
                                 (new_tag, role, p.guid))
                    conn.commit()
                    
                    p.clan_tag = new_tag
                    p.role = role
                    p.clan_group = "DEFAULT"
                    
                    self.send_rcon(f'svtell {p.id} "{msg}"')

        if cmd[0] == "!dclandisband":
            if p.clan_tag == "NONE" or p.role != "OWNER":
                self.send_rcon(f'svtell {p.id} "^1Error: ^7Only the Clan OWNER can disband the clan."')
                return

            # Check if they are already in the confirmation phase
            if p.guid in self.pending_disbands:
                # 2nd Time: Execute the disband
                target_tag = p.clan_tag
                with sqlite3.connect(self.db_filename) as conn:
                    conn.execute("UPDATE players SET clan_tag='NONE', clan_role='MEMBER', clan_group='DEFAULT' WHERE clan_tag=?", (target_tag,))
                    conn.commit()

                # Update memory for everyone in the clan
                for member in self.players:
                    if member.clan_tag == target_tag:
                        member.clan_tag, member.role, member.clan_group = "NONE", "MEMBER", "DEFAULT"

                del self.pending_disbands[p.guid]
                self.send_rcon(f'say "^5[CLAN] ^3{target_tag} ^7has been officially disbanded by ^5{p.name}^7."')
            
            else:
                # 1st Time: Ask for confirmation
                self.pending_disbands[p.guid] = time.time()
                self.send_rcon(f'svtell {p.id} "^1WARNING: ^7This will remove ALL members from ^3{p.clan_tag}^7."')
                self.send_rcon(f'svtell {p.id} "^7Type ^2!dclandisband ^7again within 10 seconds to confirm."')
                
                # Optional: Simple timer to clear the pending status
                threading.Timer(10, lambda: self.pending_disbands.pop(p.guid, None)).start()            

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
            target = p
            if len(cmd) > 1:
                target_search = " ".join(cmd[1:]).lower()
                target = next((x for x in self.players if target_search in x.clean_name), p)

            with sqlite3.connect(self.db_filename) as conn:
                cursor = conn.cursor()
                # Search by GUID first, or by clean_name if GUID is "0"
                if target.guid == "0" or not target.guid:
                    cursor.execute("""SELECT duel_rating, total_rounds_won, tournament_wins, name 
                                   FROM players WHERE clean_name = ? ORDER BY rowid DESC""", (target.clean_name,))
                else:
                    cursor.execute("""SELECT duel_rating, total_rounds_won, tournament_wins, name 
                                   FROM players WHERE guid = ?""", (target.guid,))
                
                data = cursor.fetchone()
                if data:
                    rating, rounds, t_wins, db_name = data
                    # Use db_name instead of target.name to avoid "Unknown"
                    rank_msg = (f"^5Rank for ^2{db_name}: ^7Rating: ^3{int(rating)} ^7| "
                                f"Rounds: ^3{rounds} ^7| Tourney Wins: ^3{t_wins}")
                    self.send_rcon(f'svtell {p.id} "{rank_msg}"')
                else:
                    # If truly not in DB, show session stats
                    self.send_rcon(f'svtell {p.id} "^5Rank for ^2{target.name}: ^7Rating: ^3{int(target.rating)} ^7| New Player"')

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
            self.win_limit = inviter.pending_limit  
            self.send_rcon(f'say "^5[DUEL] ^2{inviter.name} ^7vs ^2{p.name} ^7started (First to {self.win_limit})!"')
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
            # REMOVED: guid != '0'
            # ADDED: duel_rating > 1500 (optional, to hide unranked)
            cursor.execute(f"""
                SELECT name, {column} 
                FROM players 
                WHERE name != 'Unknown' AND name != ''
                ORDER BY {column} DESC LIMIT 5
            """)
            rows = cursor.fetchall()
            
            self.send_rcon(f'svtell {sid} "^5--- TOP 5 {label} ---"')
            if not rows:
                self.send_rcon(f'svtell {sid} "^7No data available yet."')
                return

            for i, (name, val) in enumerate(rows, 1):
                self.send_rcon(f'svtell {sid} "^7{i}. ^2{name} ^7- ^3{int(val)}"')

    def show_clan_leaderboard(self, sid):
        with sqlite3.connect(self.db_filename) as conn:
            cursor = conn.cursor()
            # Removed guid != '0' to ensure Slot 0 and new players are counted
            cursor.execute("""
                SELECT clan_tag, AVG(duel_rating) as avg_r 
                FROM players 
                WHERE clan_tag != 'NONE' 
                AND clan_tag != ''
                AND name != 'Unknown'
                GROUP BY clan_tag 
                ORDER BY avg_r DESC LIMIT 5
            """)
            rows = cursor.fetchall()
            
            self.send_rcon(f'svtell {sid} "^5--- TOP 5 CLANS (Avg Rating) ---"')
            if not rows:
                self.send_rcon(f'svtell {sid} "^7No clans found."')
                return

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
        print(f"[SYSTEM] Plugin active. Monitoring {log}")

        self.is_catching_up = True 

        while True:
            try:
                if not os.path.exists(log):
                    time.sleep(1)
                    continue

                curr_sz = os.path.getsize(log)
                if curr_sz < last_sz:
                    last_sz = 0

                if curr_sz > last_sz:
                    with open(log, 'r', encoding='utf-8', errors='ignore') as f:
                        f.seek(last_sz)
                        # 1. Read everything into a list so we can close the file safely
                        lines = f.readlines()
                        # 2. Update bookmark immediately after reading
                        last_sz = f.tell()

                    # 3. Process the lines using the LIST, not the file handle 'f'
                    for line in lines:
                        line = line.strip()
                        if not line: continue

                        if "InitGame:" in line:
                            self.send_rcon('say "^5[SYSTEM] ^7Map/Round change detected. Restoring states..."')
                            # Clear temporary local lists but keep the database-backed players
                            self.lobby_players = []
                            self.active_tournament = False
                            continue

                        # --- THE SWITCHBOARD (Keep this updated!) ---
                        # Capture name changes (ClientUserinfoChanged: 0 n\zaanne\t\...)
                        m_info = re.search(r'ClientUserinfoChanged: (\d+) n\\(.*?)\\t\\', line)
                        if m_info:
                            slot_id = int(m_info.group(1))
                            full_name = m_info.group(2).strip()
                            
                            # sync_player gets the player from DB/List
                            p = self.sync_player(slot_id, full_name, "0") 
                            
                            # PLUG INTO SWITCHBOARD
                            self.slot_map[slot_id] = p 
                            print(f"[DEBUG] Slot {slot_id} is now mapped to {p.clean_name}")
                            continue

                        # Capture GUIDs (Player 0: zaanne ja_guid\ABC...)
                        m_spawn = re.search(r'(?:Player|ClientInfo)\s+(\d+).*?ja_guid\\([A-Z0-9]{32})', line)
                        if m_spawn:
                            sid = int(m_spawn.group(1))
                            guid = m_spawn.group(2)
                            for p in self.players:
                                if p.guid == guid:
                                    p.id = sid  
                                    self.slot_map[sid] = p # PLUG INTO SWITCHBOARD
                                    break

                        # 3. DUEL LOGIC (Start)
                        m_start = re.search(r'DuelStart: (.*?) challenged (.*?) to a private duel', line)
                        if m_start:
                            raw_p1, raw_p2 = m_start.group(1).strip(), m_start.group(2).strip()
                            
                            # Try to find existing players in memory first to preserve their real Slot IDs
                            p1 = next((x for x in self.players if x.clean_name == normalize(raw_p1)), None)
                            p2 = next((x for x in self.players if x.clean_name == normalize(raw_p2)), None)

                            # Fallback to sync if they aren't in memory
                            if not p1: p1 = self.sync_player(0, raw_p1, "0")
                            if not p2: p2 = self.sync_player(0, raw_p2, "0")

                            if p1 and p2:
                                # THE BUSY CHECK: 
                                # Check if these names are already inside any active duel key
                                is_p1_busy = any(p1.clean_name in key for key in self.active_duels)
                                is_p2_busy = any(p2.clean_name in key for key in self.active_duels)

                                if is_p1_busy or is_p2_busy:
                                    # Silently ignore the second start log to prevent double-challenges
                                    continue

                                duel_key = tuple(sorted([p1.clean_name, p2.clean_name]))
                                self.active_duels.add(duel_key)
                                
                                if not self.is_catching_up:
                                    self.send_rcon(f'say "^5[DUEL] ^7Challenge: ^2{p1.name} ^7(^5{int(p1.rating)}^7) vs ^2{p2.name} ^7(^5{int(p2.rating)}^7)"')
                            continue

                        # 4. DUEL LOGIC (End)
                        m_end = re.search(r'DuelEnd:\s+(.*?)\s+has defeated\s+(.*?)\s+in a private duel', line)
                        if m_end:
                            raw_w, raw_l = m_end.group(1).strip(), m_end.group(2).strip()
                            winner, loser = self.sync_player(0, raw_w, "0"), self.sync_player(0, raw_l, "0")

                            if winner and loser:
                                duel_key = tuple(sorted([winner.clean_name, loser.clean_name]))
                                if duel_key in self.active_duels:
                                    self.active_duels.discard(duel_key)
                                    
                                    # Always update ratings
                                    self.calculate_glicko2(winner, loser)

                                    if not self.is_catching_up:
                                        self.send_rcon(f'say "^5[DUEL] ^2{winner.name} ^7wins! ^2{int(winner.rating)} | ^2{loser.name} ^7dropped to ^1{int(loser.rating)}"')

                                    # ONLY Track Rounds if they are in a formal !dduel match
                                    if winner.opponent == loser:
                                        winner.match_score += 1
                                        
                                        # Update round stats in DB ONLY for formal matches
                                        with sqlite3.connect(self.db_filename) as conn:
                                            w_valid = winner.guid and winner.guid != "0" and len(winner.guid) > 10
                                            l_valid = loser.guid and loser.guid != "0" and len(loser.guid) > 10
                                            
                                            conn.execute(f"UPDATE players SET total_rounds_won = total_rounds_won + 1 WHERE {'guid' if w_valid else 'clean_name'}=?", 
                                                         (winner.guid if w_valid else winner.clean_name,))
                                            conn.execute(f"UPDATE players SET total_rounds_lost = total_rounds_lost + 1 WHERE {'guid' if l_valid else 'clean_name'}=?", 
                                                         (loser.guid if l_valid else loser.clean_name,))
                                            conn.commit()

                                        self.save_match_progress(winner, loser)
                                        
                                        if not self.is_catching_up:
                                            self.send_rcon(f'say "^5[MATCH] ^2{winner.name} ^7({winner.match_score}/{self.win_limit}) vs ^2{loser.name} ^7({loser.match_score}/{self.win_limit})" ')
                                        
                                        if winner.match_score >= self.win_limit:
                                            self.finalize_match(winner, loser)
                            continue

                        # 5. DISCONNECT CLEANUP (Removed 'entered the game')
                        elif "ClientDisconnect:" in line:
                            m = re.search(r'ClientDisconnect:\s*(\d+)', line)
                            if m:
                                t_sid = int(m.group(1)) # Group 1 because the regex is now simpler
                                t_p = next((x for x in self.players if x.id == t_sid), None)
                                
                                if t_p:
                                    # --- THE FORFEIT LOGIC ---
                                    if t_p.opponent:
                                        opp = t_p.opponent
                                        if not self.is_catching_up:
                                            self.send_rcon(f'say "^5[MATCH] ^2{opp.name} ^7wins! ^2{t_p.name} ^7left the server."')
                                        opp.opponent = None
                                        opp.match_score = 0

                                    # --- CLEAR ACTIVE DUEL GATE ---
                                    # Use a set comprehension to filter out the leaving player's name
                                    self.active_duels = {key for key in self.active_duels if t_p.clean_name not in key}
                                    
                                    # --- SESSION REMOVAL ---
                                    # This ensures that if they rejoin, sync_player creates a fresh object
                                    self.players = [p for p in self.players if p.id != t_sid]  

                        # --- SMOD ADMIN PARSER ---
                        if "SMOD smsay:" in line:
                            # DEBUG 1: Verify the script picked up the SMOD trigger
                            # print(f"[DEBUG] SMOD line detected: {line.strip()}") 

                            # Regex tailored to your debug log: handles the "):" without a space
                            smod_match = re.search(r'SMOD smsay:\s+(.*?)\s+\(adminID:\s+(\d+)\).*?\):\s*(.*)$', line)
                            
                            if smod_match:
                                admin_raw_name = smod_match.group(1).strip()
                                admin_id = smod_match.group(2)
                                full_message = smod_match.group(3).strip()
                                
                                # DEBUG 2: Verify the regex captured the correct groups
                                # print(f"[DEBUG] Regex Match Success! Admin: {admin_raw_name}, ID: {admin_id}, Msg: {full_message}")
                                
                                self.handle_smod_command(admin_raw_name, admin_id, full_message)
                            else:
                                # DEBUG 3: If the line was seen but regex failed
                                # print(f"[DEBUG] Regex FAILED to match SMOD line format.")
                                pass

                        elif "say:" in line.lower():
                            try:
                                # 1. Extract raw SID string
                                sid_match = re.search(r'(\d+)[:\s]*say:', line, re.IGNORECASE)
                                msg_match = re.search(r'say:\s*(.*?):\s*"(.*)"', line)
                                
                                if sid_match and msg_match:
                                    sid_str = sid_match.group(1)
                                    raw_name = msg_match.group(1)
                                    message = msg_match.group(2).strip()

                                    # 2. USE CENTRALIZED LOGIC FOR SLOT 0
                                    log_sid = self.update_player_slot(sid_str, raw_name)
                                    
                                    # 3. Use 'is not None' to allow Slot 0
                                    p = next((x for x in self.players if x.id == log_sid), None)
                                    
                                    if p is not None:
                                        p.id = log_sid # Sync mashed ID to clean ID
                                        self.handle_chat(p, message)
                                    else:
                                        # Fallback for sync failures
                                        p_temp = Player(log_sid, raw_name, "0")
                                        self.handle_chat(p_temp, message)
                            except Exception as e:
                                print(f"[PARSER ERROR] Say line failed: {e}")

                        elif "tell:" in line.lower():
                            try:
                                sid_match = re.search(r'(\d+)\s*tell:', line, re.IGNORECASE)
                                # Captures the sender name and the message
                                msg_match = re.search(r'tell:\s*(.*?)\s+to\s+.*?:\s*"(.*)"', line)
                                
                                if sid_match and msg_match:
                                    sid_str = sid_match.group(1)
                                    raw_sender = msg_match.group(1).strip()
                                    message = msg_match.group(2).strip()

                                    # Sync Slot 0 via update_player_slot
                                    log_sid = self.update_player_slot(sid_str, raw_sender)

                                    p = next((x for x in self.players if x.id == log_sid), None)
                                    
                                    # Safe fallback for Slot 0
                                    if p is None:
                                        clean_n = normalize(raw_sender).lower().replace('[','').replace(']','').strip()
                                        p = next((x for x in self.players if x.clean_name == clean_n), None)

                                    if p is not None:
                                        p.id = log_sid
                                        self.handle_chat(p, message)
                                    else:
                                        p_temp = Player(log_sid, raw_sender, "0")
                                        self.handle_chat(p_temp, message)
                            except Exception as e:
                                print(f"[PARSER ERROR] Tell line failed: {e}")    

                    if self.is_catching_up:
                        self.is_catching_up = False
                        print("[SYSTEM] Catch-up complete. Live monitoring enabled.")


            except Exception as e:
                print(f"[CRITICAL ERROR] Loop failure: {e}")

            time.sleep(0.1)

    def sync_player(self, sid, name, guid):
        valid_guid = guid and guid != "0" and len(guid) > 10
        current_name = name
        current_clean = normalize(name).lower().replace('[', '').replace(']', '').strip()
        
        rating, rd, role, group = 1500, 350, "MEMBER", "DEFAULT"
        clan = self.detect_clan(name)
        
        with sqlite3.connect(self.db_filename) as conn:
            cursor = conn.cursor()
            
            # 1. PRIORITY SEARCH: Check for the GUID first
            data = None
            if valid_guid:
                cursor.execute("SELECT duel_rating, rating_deviation, clan_tag, clan_role, clan_group, clean_name FROM players WHERE guid = ?", (guid,))
                data = cursor.fetchone()

            # 2. FALLBACK SEARCH: If no GUID match, try the Name
            if not data:
                cursor.execute("SELECT duel_rating, rating_deviation, clan_tag, clan_role, clan_group, clean_name FROM players WHERE clean_name = ?", (current_clean,))
                data = cursor.fetchone()
            
            if data:
                # 3. MERGE/UPDATE: We found them (either by GUID or Name)
                rating, rd, db_clan, role, group, old_clean_name = data
                
                if valid_guid:
                    # If we found them by Name but the DB had no GUID (or a different one), 
                    # OR if they just changed their name, this syncs everything to the current GUID.
                    conn.execute("""UPDATE players SET guid=?, name=?, clean_name=? 
                                 WHERE guid=? OR clean_name=?""", 
                                 (guid, current_name, current_clean, guid, current_clean))
            else:
                # 4. NEW PLAYER: Truly a new face
                conn.execute("""INSERT OR IGNORE INTO players (guid, name, clean_name, clan_tag, duel_rating, rating_deviation) 
                             VALUES (?, ?, ?, ?, ?, ?)""", 
                             (guid if valid_guid else f"TEMP_{current_clean}", current_name, current_clean, clan, rating, rd))
            
            conn.commit()

        # Update live memory
        self.players = [p for p in self.players if p.id != sid and p.clean_name != current_clean]
        new_player = Player(sid, name, guid, rating, rd, clan=clan, role=role, group=group)
        self.players.append(new_player)
        return new_player

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