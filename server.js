const express = require('express');
const http = require('http');
const WebSocket = require('ws');
const path = require('path');

const app = express();
const server = http.createServer(app);
const wss = new WebSocket.Server({ server });

app.use(express.static(path.join(__dirname, 'public')));

// Game sessions storage
const sessions = new Map();
const playerSessions = new Map();

function generateCode() {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  let code = '';
  for (let i = 0; i < 5; i++) code += chars[Math.floor(Math.random() * chars.length)];
  return code;
}

function createGameState() {
  return {
    players: {},
    projectiles: [],
    grenades: [],
    tick: 0,
    started: false,
    weather: 'clear',
    worldWidth: 1600,
    worldHeight: 600,
  };
}

wss.on('connection', (ws) => {
  let playerId = Math.random().toString(36).substr(2, 9);
  ws.playerId = playerId;

  ws.on('message', (raw) => {
    let msg;
    try { msg = JSON.parse(raw); } catch (e) { return; }

    switch (msg.type) {
      case 'create_session': {
        const code = generateCode();
        const state = createGameState();
        state.players[playerId] = {
          id: playerId,
          side: 'allies',
          x: 120, y: 400,
          health: 100,
          ammo: 30,
          grenades: 3,
          alive: true,
          facing: 'right',
          inTrench: false,
          shooting: false,
          crouching: false,
          vx: 0, vy: 0,
          kills: 0, deaths: 0,
          weapon: 'rifle',
          reloading: false,
          lastShot: 0,
        };
        sessions.set(code, { state, players: [ws], spectators: [] });
        playerSessions.set(playerId, code);
        ws.send(JSON.stringify({ type: 'session_created', code, playerId, side: 'allies' }));
        break;
      }

      case 'join_session': {
        const session = sessions.get(msg.code);
        if (!session) {
          ws.send(JSON.stringify({ type: 'error', message: 'Session not found' }));
          break;
        }
        if (session.players.length >= 2) {
          ws.send(JSON.stringify({ type: 'error', message: 'Session is full' }));
          break;
        }
        session.state.players[playerId] = {
          id: playerId,
          side: 'axis',
          x: 1480, y: 400,
          health: 100,
          ammo: 30,
          grenades: 3,
          alive: true,
          facing: 'left',
          inTrench: false,
          shooting: false,
          crouching: false,
          vx: 0, vy: 0,
          kills: 0, deaths: 0,
          weapon: 'rifle',
          reloading: false,
          lastShot: 0,
        };
        session.players.push(ws);
        playerSessions.set(playerId, msg.code);
        session.state.started = true;
        // Notify both players
        session.players.forEach(p => {
          p.send(JSON.stringify({
            type: 'game_start',
            state: session.state,
            playerId: p.playerId,
            side: session.state.players[p.playerId].side
          }));
        });
        startGameLoop(msg.code);
        break;
      }

      case 'input': {
        const code = playerSessions.get(playerId);
        if (!code) break;
        const session = sessions.get(code);
        if (!session) break;
        const player = session.state.players[playerId];
        if (!player || !player.alive) break;

        const inp = msg.input;
        const speed = player.crouching ? 1.5 : 3;

        if (inp.left) { player.vx = -speed; player.facing = 'left'; }
        else if (inp.right) { player.vx = speed; player.facing = 'right'; }
        else { player.vx = 0; }

        if (inp.jump && player.y >= 390) { player.vy = -8; }
        player.crouching = inp.crouch || false;

        if (inp.shoot) {
          const now = Date.now();
          const fireRate = player.weapon === 'rifle' ? 600 : player.weapon === 'smg' ? 150 : 800;
          if (now - player.lastShot > fireRate && player.ammo > 0 && !player.reloading) {
            player.lastShot = now;
            player.ammo--;
            const bulletSpeed = 12;
            const dir = player.facing === 'right' ? 1 : -1;
            const spread = player.weapon === 'smg' ? (Math.random() - 0.5) * 3 : (Math.random() - 0.5) * 0.5;
            session.state.projectiles.push({
              x: player.x + dir * 10,
              y: player.y - (player.crouching ? 5 : 12),
              vx: bulletSpeed * dir,
              vy: spread,
              owner: playerId,
              damage: player.weapon === 'rifle' ? 35 : player.weapon === 'smg' ? 12 : 50,
            });
          }
        }

        if (inp.grenade && player.grenades > 0) {
          player.grenades--;
          const dir = player.facing === 'right' ? 1 : -1;
          session.state.grenades.push({
            x: player.x + dir * 10,
            y: player.y - 20,
            vx: 5 * dir,
            vy: -6,
            owner: playerId,
            timer: 120,
            exploded: false,
          });
        }

        if (inp.reload && player.ammo < 30 && !player.reloading) {
          player.reloading = true;
          setTimeout(() => {
            player.ammo = 30;
            player.reloading = false;
          }, 2000);
        }

        if (inp.weapon) {
          player.weapon = inp.weapon;
        }
        break;
      }

      case 'find_match': {
        // Simple matchmaking - find open session or create one
        let found = false;
        for (const [code, session] of sessions) {
          if (session.players.length === 1 && !session.state.started) {
            ws.send(JSON.stringify({ type: 'match_found', code }));
            found = true;
            break;
          }
        }
        if (!found) {
          // Auto-create and wait
          const code = generateCode();
          const state = createGameState();
          state.players[playerId] = {
            id: playerId, side: 'allies',
            x: 120, y: 400, health: 100, ammo: 30, grenades: 3,
            alive: true, facing: 'right', inTrench: false,
            shooting: false, crouching: false, vx: 0, vy: 0,
            kills: 0, deaths: 0, weapon: 'rifle', reloading: false, lastShot: 0,
          };
          sessions.set(code, { state, players: [ws], spectators: [] });
          playerSessions.set(playerId, code);
          ws.send(JSON.stringify({ type: 'waiting_match', code, playerId, side: 'allies' }));
        }
        break;
      }
    }
  });

  ws.on('close', () => {
    const code = playerSessions.get(playerId);
    if (code) {
      const session = sessions.get(code);
      if (session) {
        session.players = session.players.filter(p => p.playerId !== playerId);
        delete session.state.players[playerId];
        if (session.players.length === 0) {
          sessions.delete(code);
        } else {
          session.players.forEach(p => {
            p.send(JSON.stringify({ type: 'player_left', playerId }));
          });
        }
      }
      playerSessions.delete(playerId);
    }
  });
});

const gameLoops = new Map();

function startGameLoop(code) {
  if (gameLoops.has(code)) return;
  const interval = setInterval(() => {
    const session = sessions.get(code);
    if (!session || session.players.length === 0) {
      clearInterval(interval);
      gameLoops.delete(code);
      return;
    }
    const state = session.state;
    state.tick++;

    // Update players
    for (const pid of Object.keys(state.players)) {
      const p = state.players[pid];
      if (!p.alive) continue;
      p.x += p.vx;
      p.vy += 0.4; // gravity
      p.y += p.vy;
      // Ground collision
      if (p.y > 400) { p.y = 400; p.vy = 0; }
      // World bounds
      if (p.x < 10) p.x = 10;
      if (p.x > state.worldWidth - 10) p.x = state.worldWidth - 10;
      // Trench detection
      p.inTrench = isInTrench(p.x, p.y);
    }

    // Update projectiles
    state.projectiles = state.projectiles.filter(b => {
      b.x += b.vx;
      b.y += b.vy;
      if (b.x < 0 || b.x > state.worldWidth || b.y < 0 || b.y > 600) return false;
      // Hit detection
      for (const pid of Object.keys(state.players)) {
        const p = state.players[pid];
        if (pid === b.owner || !p.alive) continue;
        const hitH = p.crouching ? 12 : 24;
        if (Math.abs(b.x - p.x) < 8 && Math.abs(b.y - (p.y - hitH / 2)) < hitH / 2) {
          const dmgMult = p.inTrench && !p.crouching ? 0.5 : 1;
          p.health -= b.damage * dmgMult;
          if (p.health <= 0) {
            p.alive = false;
            p.health = 0;
            state.players[b.owner].kills++;
            p.deaths++;
            // Respawn after 3s
            setTimeout(() => {
              if (state.players[pid]) {
                p.alive = true;
                p.health = 100;
                p.ammo = 30;
                p.grenades = 3;
                p.x = p.side === 'allies' ? 120 : 1480;
                p.y = 400;
              }
            }, 3000);
          }
          return false;
        }
      }
      return true;
    });

    // Update grenades
    state.grenades = state.grenades.filter(g => {
      if (g.exploded) return false;
      g.x += g.vx;
      g.vy += 0.3;
      g.y += g.vy;
      g.vx *= 0.98;
      if (g.y > 410) { g.y = 410; g.vy = -g.vy * 0.3; g.vx *= 0.7; }
      g.timer--;
      if (g.timer <= 0) {
        g.exploded = true;
        // Explosion damage
        for (const pid of Object.keys(state.players)) {
          const p = state.players[pid];
          if (!p.alive) continue;
          const dist = Math.sqrt((p.x - g.x) ** 2 + (p.y - g.y) ** 2);
          if (dist < 80) {
            const dmg = Math.floor(80 * (1 - dist / 80));
            p.health -= dmg;
            if (p.health <= 0) {
              p.alive = false;
              p.health = 0;
              if (g.owner !== pid) state.players[g.owner].kills++;
              p.deaths++;
              setTimeout(() => {
                if (state.players[pid]) {
                  p.alive = true; p.health = 100; p.ammo = 30; p.grenades = 3;
                  p.x = p.side === 'allies' ? 120 : 1480;
                  p.y = 400;
                }
              }, 3000);
            }
          }
        }
        // Send explosion event
        session.players.forEach(pw => {
          pw.send(JSON.stringify({ type: 'explosion', x: g.x, y: g.y }));
        });
        return false;
      }
      return true;
    });

    // Send state to all players
    session.players.forEach(pw => {
      pw.send(JSON.stringify({ type: 'state', state }));
    });
  }, 1000 / 30); // 30 FPS server tick
  gameLoops.set(code, interval);
}

// Trench positions (x ranges where trenches exist)
const trenches = [
  { x1: 60, x2: 180, side: 'allies' },
  { x1: 350, x2: 450, side: 'allies' },
  { x1: 700, x2: 900, side: 'neutral' },
  { x1: 1150, x2: 1250, side: 'axis' },
  { x1: 1420, x2: 1540, side: 'axis' },
];

function isInTrench(x, y) {
  if (y < 390) return false;
  for (const t of trenches) {
    if (x >= t.x1 && x <= t.x2) return true;
  }
  return false;
}

const PORT = process.env.PORT || 3000;
server.listen(PORT, () => {
  console.log(`\n====================================`);
  console.log(`  TRENCH WAR server running`);
  console.log(`  http://localhost:${PORT}`);
  console.log(`====================================\n`);
});
