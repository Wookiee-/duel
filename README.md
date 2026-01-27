# MBII Duel & Clan Management Plugin

![Python Version](https://img.shields.io/badge/python-3.8+-blue.svg)
![Database](https://img.shields.io/badge/database-SQLite3-lightgrey.svg)
![Platform](https://img.shields.io/badge/platform-Movie%20Battles%20II-orange.svg)

An automated RCON management system for **Movie Battles II** (Jedi Academy). This plugin provides a comprehensive competitive framework featuring Glicko-2 skill ratings, automated tournaments, and a persistent hierarchical clan system using real-time log tailing and SQLite.

---

## 🚀 Key Features

### 1. Robust Persistence & Recovery
* **Match Restoration**: Automatically saves match scores to the `active_matches` table. If the map changes (15-minute MB2 limit) or the server restarts, the plugin re-links opponents and restores their scores upon reconnection.
* **Global Error Handling**: A top-level wrapper catches runtime exceptions, performs an emergency database save of all player ratings, and restarts the plugin automatically within 5 seconds.
* **InitGame Integration**: Uses the `InitGame:` log trigger to reset session-specific variables while maintaining database-backed persistence.

### 2. Hierarchical Clan & Role System
* **Four-Tier Roles**: Implements a granular authority system: `MEMBER` < `OFFICER` < `LEADER` < `OWNER`.
* **Squad Management**: Allows clans to be organized into subdivisions (e.g., "Alpha Squad") with independent "Locked" or "Open" join statuses.
* **Staff Controls**: Automated rank checks prevent `MEMBER` rank players from initiating `!tstart` or administrative functions.

### 3. Competitive Systems & Rating
* **Glicko-2 Rating Algorithm**: Dynamic skill calculation based on opponent strength and rating deviation.
* **Automated Match Detection**: Monitors logs for private duels to award rating points and update leaderboards automatically.
* **Tournament Suite**: Automated bracket seeding based on rating, lobby management, and round transitions.
* **Clean Exit Logic**: Natural wins and forfeits (`!dforfeit`/`!tforfeit`) automatically clear the database to prevent accidental score restoration.

### 4. Global Leaderboards
* **!rank**: Provides a comprehensive personal summary of Rating, Total Rounds Won, and Tournament Wins.
* **Individual Top 5**: Dedicated leaderboards for rating (`!dtop`), rounds won (`!fttop`), and tourney wins (`!ttop`).
* **Clan Rankings**: Displays the top clans based on the average Glicko-2 rating of all active members (`!dclantop`).

### 🛡️ Robust Chat Parsing
* **SID De-mashing**: Automatically handles Jedi Academy's log behavior where timestamps and player IDs are concatenated (e.g., `314: say:`).
* **Double-Lookup Strategy**: Uses a primary Server ID check with a secondary normalized Name-Search fallback to ensure commands never fail due to color codes or hidden characters.
* **Fail-safe Execution**: Unknown players are automatically assigned temporary session profiles so they can still access help commands and leaderboard lookups.

---

## 🎮 Player Commands

| Category | Command | Description |
| :--- | :--- | :--- |
| **Stats** | `!rank [name]` | View combined Rating, Rounds, and Tourney Wins. |
| **Stats** | `!dtop` / `!fttop` | View Top 5 by Rating or Total Rounds Won. |
| **Duel** | `!dduel <n> [r]` | Challenge a player to a "First to X" match. |
| **Duel** | `!dpause` / `!dresume` | Request or accept a match pause. |
| **Duel** | `!dforfeit` | Surrender current match and clear persistent data. |
| **Tourney** | `!thelp` | View all tournament-specific commands. |
| **Clan** | `!dclantag register <T>` | Joins/Creates a clan. First member becomes **OWNER**. |

---

### 🛡️ Admin & Staff Operations (SMOD)
*These commands are sent via admin chat or svtell and bypass standard player restrictions to maintain server order.*

#### 👑 Senior Admin Commands (SMOD)
* **`!clan <name> <TAG>`**: Overrides a player's current clan affiliation and updates the database.
* **`!group <name> <group>`**: Force-assigns a player to a specific squad or subdivision (e.g., ALPHA).
* **`!promote <name>`**: Instantly advances a player to the **OWNER** rank within their registered clan.
* **`!resetplayer <name>`**: Completely wipes a player's Glicko-2 ratings, round history, and tournament wins from the database.

#### ⚔️ Tournament & Match Control
* **`!cstart`**: Manually initializes the competitive match state tracker.
* **`!tstart <score>`**: Opens a tournament lobby; defaults to First to 5 if no score is specified.
* **`!tpause` / `!tresume`**: Globally halts or resumes all active tournament matches—useful for server-wide timeouts or technical issues.

#### 💬 Admin Intelligence
* **`!admin_ops`**: Displays a private summary of all high-level administrative commands to the caller's console.

---

## ⚙️ Setup and Installation

1. **Configuration**: Create or edit `duel.cfg` with your server details:
   ```ini
   [SETTINGS]
   ip = 127.0.0.1
   port = 29070
   rcon = your_password
   logname = path/to/server.log
   db_file = duel.db
    ```
2.  **Database**: The script will automatically create `duel.db` on its first run.

## 🚀 Automated Execution Scripts

The repository includes management scripts to run the plugin in the background or as a persistent service.

### 🪟 Windows Setup
1. Ensure **Python 3** is in your System PATH.
2. Double-click `start_duel.bat`.
3. Select **1** to start the plugin minimized.
4. Use **4** to verify it is running.

### 🐧 Linux Setup
1. Give the script execution permissions:
   ```bash
   chmod +x start_duel.sh 

   ./start_duel.sh start | stop | restart | status
    ```

---

## 🛠 Requirements
* **Python 3.x**
* **SQLite3**
* **Movie Battles II Server** with RCON and logging enabled.
* **Works with [mbiided with Duel Isolation](https://github.com/Wookiee-/MB2OpenJK/releases/tag/Duel)**

