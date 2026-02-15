# ⚔ TRENCH WAR ⚔
## WW2 Pixel Art Trench Warfare — 1v1 Browser Multiplayer

A real-time multiplayer WW2 trench warfare game with pixel art graphics. Play 1v1 against friends or find random opponents online.

### Features
- **Real-time 1v1 multiplayer** via WebSockets
- **Room codes** — create a room and share the 5-letter code
- **Matchmaking** — find random opponents instantly
- **3 weapons** — Rifle, SMG, Sniper
- **Grenades** with physics and explosion effects
- **Trench cover system** — take less damage in trenches
- **Pixel art rendering** with parallax backgrounds, weather effects, and particle systems

### Controls
- **WASD / Arrow Keys** — Move & Jump
- **Mouse Click** — Shoot
- **G** — Throw Grenade
- **R** — Reload
- **C** — Crouch
- **1/2/3** — Switch to Rifle / SMG / Sniper

### Running Locally
```bash
pip install -r requirements.txt
python server.py
```
Then open http://localhost:3000

### Deploy to Render.com (Free)
1. Push this folder to a GitHub repo
2. Go to https://render.com and sign in with GitHub
3. Click "New" → "Web Service"
4. Connect your repo
5. Settings will auto-detect from `render.yaml`
6. Click "Create Web Service"
7. Your game will be live at `https://your-app.onrender.com`
