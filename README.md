# MBII Duel & Clan Management Plugin

An automated RCON management system for Movie Battles II (Jedi Academy). This plugin handles Glicko-2 skill ratings, automated tournaments, and a comprehensive hierarchical clan system using real-time log tailing and SQLite persistence.

---

## 🚀 Detailed Features & Functionality

### 1. Player Management and Data Persistence
* **Real-time Synchronization**: Tracks players as they join and spawn, identifying them by their unique `ja_guid`.
* **Database Integration**: Maintains a SQLite database (`duel.db`) to store persistent player information, including ratings, clan roles, and total wins.
* **Name Normalization**: Strips Jedi Academy color codes and special characters from names to ensure consistent tracking and searching.
* **Automatic Updates**: Detects when a player changes their name on the server and automatically updates the corresponding database record.

### 2. Hierarchical Clan System
* **Clan Registration**: Allows players to register their own clans using `!dclantag register`, automatically assigning the first registrant as the **LEADER**.
* **Role Management**: Supports different authority levels, specifically **LEADER** and **MEMBER**.
* **Subdivision/Squad Control**: Enables clans to be organized into smaller groups or squads (e.g., "Alpha Squad").
* **Leader Authorities**: Clan Leaders can promote members to leaders, kick members, and rename their clan's subdivisions.
* **Locked Divisions**: Leaders can "lock" specific subdivisions, requiring them to manually `!daccept` or `!ddecline` requests from members trying to join that group.
* **Roster Display**: Provides a `!dclan show` command to view all current clan members and their respective roles/squads.



### 3. Competitive Systems and Skill Rating
* **Glicko-2 Rating Algorithm**: Implements the Glicko-2 system to calculate dynamic player skill ratings based on duel outcomes.
* **Automatic Match Detection**: Monitors logs for private duel completions to automatically award rating points to winners and deduct them from losers.
* **Tournament Automation**: Includes a full tournament suite including lobby creation (`!tstart`), automated bracket seeding based on rating, and round management.
* **Challenge System**: Allows players to challenge others to "First to X" matches with custom win limits using `!dduel`.

### 4. Global Leaderboards
* **Individual Leaderboards**: Provides commands to view the top 5 players for overall rating (`!dtop`), total rounds won (`!fttop`), and tournament victories (`!ttop`).
* **Clan Rankings**: Calculates and displays the top 5 clans based on the average Glicko-2 rating of their members (`!dclantop`).

### 5. Administrative Overrides (SMOD)
* **Force Clan Management**: Allows server admins to force-set a player's clan tag or move them into/out of specific clan groups regardless of clan rules.
* **Stat Resets**: Admins can completely reset a player's duel ratings, rounds won, and tournament wins using `!resetplayer`.
* **Promotion Overrides**: Admins can promote any player to a clan leader position.

### 6. Technical Infrastructure
* **UDP RCON Communication**: Sends commands back to the game server using UDP packets with the necessary Quake-engine headers.
* **Log Tailing**: Efficiently monitors the server log file by only reading new data since the last check to minimize system impact.
* **Configuration Support**: Loads server IP, port, RCON password, and log file locations from an external configuration file (`duel.cfg`).

---

## 🎮 Player Commands

| Category | Command | Description |
| :--- | :--- | :--- |
| **Clan** | `!dclantag register <TAG>` | Joins/Creates a clan. First member becomes **LEADER**. |
| **Clan** | `!dclantag unregister` | Leaves current clan and resets status. |
| **Clan** | `!dclan show` | Displays the current clan roster, roles, and subdivisions. |
| **Clan** | `!dclan join group <name>` | Joins a group or sends a request if the group is locked. |
| **Duel** | `!dduel <name> [rounds]` | Challenges a player to a match (Default: 5 rounds). |
| **Duel** | `!dyes` / `!dno` | Accepts or declines a pending challenge. |
| **Stats** | `!dtop` / `!fttop` | View Top 5 by Rating or Total Rounds Won. |
| **Stats** | `!dclantop` | View Top 5 Clans by Average Rating. |

---

## 👑 Leader & Admin Commands

### Clan Leader Commands
*Requires **LEADER** role.*
* `!dclan promote <name>`: Promotes a member to Leader.
* `!dclan kick <name>`: Removes a player from the clan.
* `!dclan rename <old> <new>`: Batch renames a subdivision for all members.
* `!dclan lock <group>`: Toggles a group between Open and Invite-Only.
* `!daccept <ID>` / `!ddecline <ID>`: Manages pending group join requests.

### SMOD Admin Commands
*Sent via admin chat. Bypasses clan restrictions.*
* `!clan <name> <TAG>`: Force-sets a player's clan affiliation.
* `!group <name> <group>`: Force-moves a player into or out of a squad.
* `!promote <name>`: Force-promotes any player to Leader.
* `!resetplayer <name>`: Wipes all Glicko stats and tournament wins.

---

## ⚙️ Setup and Installation

1.  **Configuration**: Create or edit `duel.cfg` with your server details:
    ```ini
    [SETTINGS]
    ip = 127.0.0.1
    port = 29070
    rcon = your_password
    logname = path/to/server.log
    db_file = duel.db
    ```
2.  **Database**: The script will automatically create `duel.db` on its first run.
3.  **Run**: Execute the script using Python:
    ```bash
    python3 duel.py duel.cfg
    ```

---

## 🛠 Requirements
* **Python 3.x**
* **SQLite3**
* **Movie Battles II Server** with RCON and logging enabled.
*  **Works with [mbiided with Duel Isolation](https://github.com/Wookiee-/MB2OpenJK/releases/tag/R21)**

