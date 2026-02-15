import asyncio
import json
import random
import string
import time
import os
from aiohttp import web
import aiohttp

# ============ GAME STATE ============
sessions = {}       # code -> session dict
player_sessions = {} # player_id -> code
player_ws = {}       # player_id -> ws

def gen_code():
    chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return ''.join(random.choice(chars) for _ in range(5))

def create_player(pid, side):
    x = 120 if side == 'allies' else 1480
    facing = 'right' if side == 'allies' else 'left'
    return {
        'id': pid, 'side': side,
        'x': x, 'y': 400, 'health': 100,
        'ammo': 30, 'grenades': 3,
        'alive': True, 'facing': facing,
        'inTrench': False, 'shooting': False,
        'crouching': False, 'vx': 0, 'vy': 0,
        'kills': 0, 'deaths': 0,
        'weapon': 'rifle', 'reloading': False,
        'lastShot': 0, 'reloadStart': 0,
    }

def create_game_state():
    return {
        'players': {},
        'projectiles': [],
        'grenades': [],
        'tick': 0,
        'started': False,
        'worldWidth': 1600,
        'worldHeight': 600,
    }

TRENCHES = [
    {'x1': 60, 'x2': 180, 'side': 'allies'},
    {'x1': 350, 'x2': 450, 'side': 'allies'},
    {'x1': 700, 'x2': 900, 'side': 'neutral'},
    {'x1': 1150, 'x2': 1250, 'side': 'axis'},
    {'x1': 1420, 'x2': 1540, 'side': 'axis'},
]

def is_in_trench(x, y):
    if y < 390:
        return False
    for t in TRENCHES:
        if t['x1'] <= x <= t['x2']:
            return True
    return False

# Track game loop tasks
game_loops = {}

def cleanup_session(code):
    """Remove dead WebSocket player_ids from a session."""
    session = sessions.get(code)
    if not session:
        return
    alive_ids = []
    for pid in session['player_ids']:
        ws = player_ws.get(pid)
        if ws and not ws.closed:
            alive_ids.append(pid)
        else:
            # Dead connection — remove from state too
            if pid in session['state']['players']:
                del session['state']['players'][pid]
            if pid in player_sessions:
                del player_sessions[pid]
            if pid in player_ws:
                del player_ws[pid]
            print(f"[CLEANUP] Removed dead player {pid} from session {code}", flush=True)
    session['player_ids'] = alive_ids
    # If session is now empty, delete it
    if len(alive_ids) == 0:
        if code in game_loops:
            game_loops[code].cancel()
            del game_loops[code]
        del sessions[code]
        print(f"[CLEANUP] Deleted empty session {code}", flush=True)

def cleanup_all_sessions():
    """Clean up all sessions with dead connections."""
    for code in list(sessions.keys()):
        cleanup_session(code)

async def broadcast(code, msg):
    session = sessions.get(code)
    if not session:
        return
    data = json.dumps(msg)
    for pid in list(session['player_ids']):
        ws = player_ws.get(pid)
        if ws and not ws.closed:
            try:
                await ws.send_str(data)
            except:
                pass

async def send_to(pid, msg):
    ws = player_ws.get(pid)
    if ws is not None and not ws.closed:
        try:
            data = json.dumps(msg)
            await ws.send_str(data)
        except Exception as e:
            print(f"[SEND ERROR] to {pid}: {e}", flush=True)
    else:
        closed = ws.closed if ws is not None else 'no_ws'
        print(f"[SEND FAIL] {pid} ws_state={closed} keys={list(player_ws.keys())}", flush=True)

async def game_loop(code):
    try:
        while code in sessions:
            session = sessions[code]
            state = session['state']
            if not state['started']:
                await asyncio.sleep(0.1)
                continue

            state['tick'] += 1
            now = time.time() * 1000

            # Update players
            for pid, p in list(state['players'].items()):
                if not p['alive']:
                    continue
                p['x'] += p['vx']
                p['vy'] += 0.4
                p['y'] += p['vy']
                if p['y'] > 400:
                    p['y'] = 400
                    p['vy'] = 0
                if p['x'] < 10:
                    p['x'] = 10
                if p['x'] > state['worldWidth'] - 10:
                    p['x'] = state['worldWidth'] - 10
                p['inTrench'] = is_in_trench(p['x'], p['y'])
                # Reload check
                if p['reloading'] and now - p['reloadStart'] > 2000:
                    p['ammo'] = 30
                    p['reloading'] = False

            # Update projectiles
            new_projectiles = []
            for b in state['projectiles']:
                b['x'] += b['vx']
                b['y'] += b['vy']
                if b['x'] < 0 or b['x'] > state['worldWidth'] or b['y'] < 0 or b['y'] > 600:
                    continue
                hit = False
                for pid2, p2 in state['players'].items():
                    if pid2 == b['owner'] or not p2['alive']:
                        continue
                    hitH = 12 if p2['crouching'] else 24
                    if abs(b['x'] - p2['x']) < 8 and abs(b['y'] - (p2['y'] - hitH / 2)) < hitH / 2:
                        dmgMult = 0.5 if (p2['inTrench'] and not p2['crouching']) else 1
                        p2['health'] -= int(b['damage'] * dmgMult)
                        if p2['health'] <= 0:
                            p2['alive'] = False
                            p2['health'] = 0
                            state['players'][b['owner']]['kills'] += 1
                            p2['deaths'] += 1
                            # Schedule respawn
                            asyncio.get_event_loop().call_later(3, respawn_player, code, pid2)
                        hit = True
                        break
                if not hit:
                    new_projectiles.append(b)
            state['projectiles'] = new_projectiles

            # Update grenades
            new_grenades = []
            for g in state['grenades']:
                if g.get('exploded'):
                    continue
                g['x'] += g['vx']
                g['vy'] += 0.3
                g['y'] += g['vy']
                g['vx'] *= 0.98
                if g['y'] > 410:
                    g['y'] = 410
                    g['vy'] = -g['vy'] * 0.3
                    g['vx'] *= 0.7
                g['timer'] -= 1
                if g['timer'] <= 0:
                    g['exploded'] = True
                    for pid2, p2 in state['players'].items():
                        if not p2['alive']:
                            continue
                        dist = ((p2['x'] - g['x'])**2 + (p2['y'] - g['y'])**2)**0.5
                        if dist < 80:
                            dmg = int(80 * (1 - dist / 80))
                            p2['health'] -= dmg
                            if p2['health'] <= 0:
                                p2['alive'] = False
                                p2['health'] = 0
                                if g['owner'] != pid2:
                                    state['players'][g['owner']]['kills'] += 1
                                p2['deaths'] += 1
                                asyncio.get_event_loop().call_later(3, respawn_player, code, pid2)
                    await broadcast(code, {'type': 'explosion', 'x': g['x'], 'y': g['y']})
                else:
                    new_grenades.append(g)
            state['grenades'] = new_grenades

            # Broadcast state
            await broadcast(code, {'type': 'state', 'state': state})
            await asyncio.sleep(1/30)
    except asyncio.CancelledError:
        pass

def respawn_player(code, pid):
    session = sessions.get(code)
    if not session:
        return
    p = session['state']['players'].get(pid)
    if not p:
        return
    p['alive'] = True
    p['health'] = 100
    p['ammo'] = 30
    p['grenades'] = 3
    p['x'] = 120 if p['side'] == 'allies' else 1480
    p['y'] = 400

# ============ WEBSOCKET HANDLER ============
async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    player_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=9))
    player_ws[player_id] = ws
    print(f"[WS] Player {player_id} connected", flush=True)
    
    try:
        async for raw in ws:
            if raw.type == aiohttp.WSMsgType.TEXT:
                try:
                    msg = json.loads(raw.data)
                except:
                    continue
                
                print(f"[MSG] {player_id}: {msg.get('type', '?')}", flush=True)
                
                if msg['type'] == 'create_session':
                    # Clean up any stale sessions first
                    cleanup_all_sessions()
                    code = gen_code()
                    state = create_game_state()
                    state['players'][player_id] = create_player(player_id, 'allies')
                    sessions[code] = {'state': state, 'player_ids': [player_id]}
                    player_sessions[player_id] = code
                    await ws.send_str(json.dumps({
                        'type': 'session_created',
                        'code': code,
                        'playerId': player_id,
                        'side': 'allies'
                    }))

                elif msg['type'] == 'join_session':
                    code = msg.get('code', '')
                    # Clean up dead connections in this session before checking
                    cleanup_session(code)
                    session = sessions.get(code)
                    if not session:
                        await ws.send_str(json.dumps({'type': 'error', 'message': 'Session not found'}))
                        continue
                    if len(session['player_ids']) >= 2:
                        await ws.send_str(json.dumps({'type': 'error', 'message': 'Session is full'}))
                        continue
                    state = session['state']
                    state['players'][player_id] = create_player(player_id, 'axis')
                    session['player_ids'].append(player_id)
                    player_sessions[player_id] = code
                    state['started'] = True
                    # Notify both
                    for pid in session['player_ids']:
                        target_ws = player_ws.get(pid)
                        if target_ws and not target_ws.closed:
                            await target_ws.send_str(json.dumps({
                                'type': 'game_start',
                                'state': state,
                                'playerId': pid,
                                'side': state['players'][pid]['side']
                            }))
                    # Start loop
                    if code not in game_loops:
                        task = asyncio.ensure_future(game_loop(code))
                        game_loops[code] = task

                elif msg['type'] == 'find_match':
                    cleanup_all_sessions()
                    found = False
                    for code, session in sessions.items():
                        if len(session['player_ids']) == 1 and not session['state']['started']:
                            await ws.send_str(json.dumps({'type': 'match_found', 'code': code}))
                            found = True
                            break
                    if not found:
                        code = gen_code()
                        state = create_game_state()
                        state['players'][player_id] = create_player(player_id, 'allies')
                        sessions[code] = {'state': state, 'player_ids': [player_id]}
                        player_sessions[player_id] = code
                        await ws.send_str(json.dumps({
                            'type': 'waiting_match',
                            'code': code,
                            'playerId': player_id,
                            'side': 'allies'
                        }))

                elif msg['type'] == 'input':
                    code = player_sessions.get(player_id)
                    if not code:
                        continue
                    session = sessions.get(code)
                    if not session:
                        continue
                    p = session['state']['players'].get(player_id)
                    if not p or not p['alive']:
                        continue
                    
                    inp = msg.get('input', {})
                    speed = 1.5 if p['crouching'] else 3
                    
                    if inp.get('left'):
                        p['vx'] = -speed
                        p['facing'] = 'left'
                    elif inp.get('right'):
                        p['vx'] = speed
                        p['facing'] = 'right'
                    else:
                        p['vx'] = 0
                    
                    if inp.get('jump') and p['y'] >= 390:
                        p['vy'] = -8
                    p['crouching'] = bool(inp.get('crouch'))
                    
                    now = time.time() * 1000
                    if inp.get('shoot'):
                        fire_rates = {'rifle': 600, 'smg': 150, 'sniper': 800}
                        rate = fire_rates.get(p['weapon'], 600)
                        if now - p['lastShot'] > rate and p['ammo'] > 0 and not p['reloading']:
                            p['lastShot'] = now
                            p['ammo'] -= 1
                            d = 1 if p['facing'] == 'right' else -1
                            spread_map = {'rifle': 0.5, 'smg': 3, 'sniper': 0.1}
                            spread = (random.random() - 0.5) * spread_map.get(p['weapon'], 0.5)
                            dmg_map = {'rifle': 35, 'smg': 12, 'sniper': 70}
                            session['state']['projectiles'].append({
                                'x': p['x'] + d * 10,
                                'y': p['y'] - (5 if p['crouching'] else 12),
                                'vx': 12 * d,
                                'vy': spread,
                                'owner': player_id,
                                'damage': dmg_map.get(p['weapon'], 35),
                            })

                    if inp.get('grenade') and p['grenades'] > 0:
                        p['grenades'] -= 1
                        d = 1 if p['facing'] == 'right' else -1
                        session['state']['grenades'].append({
                            'x': p['x'] + d * 10,
                            'y': p['y'] - 20,
                            'vx': 5 * d,
                            'vy': -6,
                            'owner': player_id,
                            'timer': 120,
                            'exploded': False,
                        })
                    
                    if inp.get('reload') and p['ammo'] < 30 and not p['reloading']:
                        p['reloading'] = True
                        p['reloadStart'] = now
                    
                    if inp.get('weapon'):
                        p['weapon'] = inp['weapon']
    
    except Exception as e:
        print(f"WS error: {e}")
    
    finally:
        # Cleanup on disconnect
        code = player_sessions.get(player_id)
        if code:
            session = sessions.get(code)
            if session:
                if player_id in session['player_ids']:
                    session['player_ids'].remove(player_id)
                if player_id in session['state']['players']:
                    del session['state']['players'][player_id]
                if len(session['player_ids']) == 0:
                    if code in game_loops:
                        game_loops[code].cancel()
                        del game_loops[code]
                    del sessions[code]
                else:
                    for pid in session['player_ids']:
                        target_ws = player_ws.get(pid)
                        if target_ws and not target_ws.closed:
                            try:
                                await target_ws.send_str(json.dumps({'type': 'player_left', 'playerId': player_id}))
                            except:
                                pass
            del player_sessions[player_id]
        if player_id in player_ws:
            del player_ws[player_id]
    
    return ws

# ============ APP SETUP ============
async def index_handler(request):
    return web.FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'public', 'index.html'))

async def health_handler(request):
    # Also show details for debugging
    session_details = {}
    for code, session in sessions.items():
        alive_ids = []
        dead_ids = []
        for pid in session['player_ids']:
            ws = player_ws.get(pid)
            if ws and not ws.closed:
                alive_ids.append(pid)
            else:
                dead_ids.append(pid)
        session_details[code] = {
            'player_ids': session['player_ids'],
            'alive': alive_ids,
            'dead': dead_ids,
            'started': session['state']['started'],
        }
    return web.json_response({
        'status': 'ok',
        'sessions': len(sessions),
        'players': len(player_sessions),
        'details': session_details,
    })

app = web.Application()

async def periodic_cleanup(app):
    """Background task to clean up stale sessions every 30 seconds."""
    while True:
        await asyncio.sleep(30)
        cleanup_all_sessions()

async def start_background_tasks(app):
    app['cleanup_task'] = asyncio.ensure_future(periodic_cleanup(app))

async def cleanup_background_tasks(app):
    app['cleanup_task'].cancel()
    await app['cleanup_task']

app.on_startup.append(start_background_tasks)
app.on_cleanup.append(cleanup_background_tasks)

app.router.add_get('/ws', websocket_handler)
app.router.add_get('/health', health_handler)
app.router.add_get('/', index_handler)
app.router.add_static('/static', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'public'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    print(f"\n====================================")
    print(f"  TRENCH WAR server running")
    print(f"  http://localhost:{port}")
    print(f"====================================\n")
    web.run_app(app, host='0.0.0.0', port=port)
