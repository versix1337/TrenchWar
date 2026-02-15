import asyncio
import json
import random
import string
import time
import os
from aiohttp import web
import aiohttp

# ============ GAME STATE ============
sessions = {}        # code -> session dict
client_sessions = {} # client_token -> code
client_ws = {}       # client_token -> ws
client_sides = {}    # client_token -> 'allies'/'axis'

def gen_code():
    chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return ''.join(random.choice(chars) for _ in range(5))

def create_player(client_token, side):
    x = 120 if side == 'allies' else 1480
    facing = 'right' if side == 'allies' else 'left'
    return {
        'id': client_token, 'side': side,
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

game_loops = {}

async def broadcast(code, msg):
    session = sessions.get(code)
    if not session:
        return
    data = json.dumps(msg)
    for token in list(session['clients']):
        ws = client_ws.get(token)
        if ws and not ws.closed:
            try:
                await ws.send_str(data)
            except:
                pass

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

            for pid, p in list(state['players'].items()):
                if not p['alive']:
                    continue
                p['x'] += p['vx']
                p['vy'] += 0.4
                p['y'] += p['vy']
                if p['y'] > 400: p['y'] = 400; p['vy'] = 0
                if p['x'] < 10: p['x'] = 10
                if p['x'] > state['worldWidth'] - 10: p['x'] = state['worldWidth'] - 10
                p['inTrench'] = is_in_trench(p['x'], p['y'])
                if p['reloading'] and now - p['reloadStart'] > 2000:
                    p['ammo'] = 30; p['reloading'] = False

            new_projectiles = []
            for b in state['projectiles']:
                b['x'] += b['vx']; b['y'] += b['vy']
                if b['x'] < 0 or b['x'] > state['worldWidth'] or b['y'] < 0 or b['y'] > 600: continue
                hit = False
                for pid2, p2 in state['players'].items():
                    if pid2 == b['owner'] or not p2['alive']: continue
                    hitH = 12 if p2['crouching'] else 24
                    if abs(b['x'] - p2['x']) < 8 and abs(b['y'] - (p2['y'] - hitH/2)) < hitH/2:
                        dmgMult = 0.5 if (p2['inTrench'] and not p2['crouching']) else 1
                        p2['health'] -= int(b['damage'] * dmgMult)
                        if p2['health'] <= 0:
                            p2['alive'] = False; p2['health'] = 0
                            state['players'][b['owner']]['kills'] += 1; p2['deaths'] += 1
                            asyncio.get_event_loop().call_later(3, respawn_player, code, pid2)
                        hit = True; break
                if not hit: new_projectiles.append(b)
            state['projectiles'] = new_projectiles

            new_grenades = []
            for g in state['grenades']:
                if g.get('exploded'): continue
                g['x'] += g['vx']; g['vy'] += 0.3; g['y'] += g['vy']; g['vx'] *= 0.98
                if g['y'] > 410: g['y'] = 410; g['vy'] = -g['vy']*0.3; g['vx'] *= 0.7
                g['timer'] -= 1
                if g['timer'] <= 0:
                    g['exploded'] = True
                    for pid2, p2 in state['players'].items():
                        if not p2['alive']: continue
                        dist = ((p2['x']-g['x'])**2 + (p2['y']-g['y'])**2)**0.5
                        if dist < 80:
                            dmg = int(80*(1-dist/80)); p2['health'] -= dmg
                            if p2['health'] <= 0:
                                p2['alive'] = False; p2['health'] = 0
                                if g['owner'] != pid2: state['players'][g['owner']]['kills'] += 1
                                p2['deaths'] += 1
                                asyncio.get_event_loop().call_later(3, respawn_player, code, pid2)
                    await broadcast(code, {'type': 'explosion', 'x': g['x'], 'y': g['y']})
                else:
                    new_grenades.append(g)
            state['grenades'] = new_grenades
            await broadcast(code, {'type': 'state', 'state': state})
            await asyncio.sleep(1/30)
    except asyncio.CancelledError:
        pass

def respawn_player(code, pid):
    session = sessions.get(code)
    if not session: return
    p = session['state']['players'].get(pid)
    if not p: return
    p['alive'] = True; p['health'] = 100; p['ammo'] = 30; p['grenades'] = 3
    p['x'] = 120 if p['side'] == 'allies' else 1480; p['y'] = 400

# ============ WEBSOCKET HANDLER ============
async def websocket_handler(request):
    ws = web.WebSocketResponse(heartbeat=20)  # Built-in ping/pong every 20s
    await ws.prepare(request)
    
    client_token = None  # Will be set by first message
    print(f"[WS] New connection opened", flush=True)
    
    try:
        async for raw in ws:
            if raw.type == aiohttp.WSMsgType.TEXT:
                try:
                    msg = json.loads(raw.data)
                except:
                    continue
                
                if msg['type'] == 'ping':
                    await ws.send_str(json.dumps({'type': 'pong'}))
                    continue
                
                # Client sends their persistent token with every message
                token = msg.get('token')
                if not token:
                    continue
                
                # Update the WS mapping for this token
                client_token = token
                client_ws[token] = ws
                
                msg_type = msg['type']
                if msg_type != 'input':
                    print(f"[MSG] {token[:8]}: {msg_type}", flush=True)
                
                if msg_type == 'create_session':
                    # If this client already has a session, clean it up first
                    old_code = client_sessions.get(token)
                    if old_code and old_code in sessions:
                        old_session = sessions[old_code]
                        if token in old_session['clients']:
                            old_session['clients'].remove(token)
                        if token in old_session['state']['players']:
                            del old_session['state']['players'][token]
                        if len(old_session['clients']) == 0:
                            if old_code in game_loops:
                                game_loops[old_code].cancel()
                                del game_loops[old_code]
                            del sessions[old_code]
                            print(f"[CLEANUP] Deleted old session {old_code}", flush=True)
                    
                    code = gen_code()
                    state = create_game_state()
                    state['players'][token] = create_player(token, 'allies')
                    sessions[code] = {'state': state, 'clients': [token], 'created': time.time()}
                    client_sessions[token] = code
                    client_sides[token] = 'allies'
                    await ws.send_str(json.dumps({
                        'type': 'session_created',
                        'code': code,
                        'playerId': token,
                        'side': 'allies'
                    }))
                    print(f"[SESSION] {token[:8]} created {code}", flush=True)

                elif msg_type == 'join_session':
                    code = msg.get('code', '').strip().upper()
                    session = sessions.get(code)
                    if not session:
                        await ws.send_str(json.dumps({'type': 'error', 'message': 'Session not found'}))
                        print(f"[JOIN] {token[:8]} tried {code} — not found. Active: {list(sessions.keys())}", flush=True)
                        continue
                    
                    # Check if already in this session (reconnect)
                    if token in session['clients']:
                        side = client_sides.get(token, 'allies')
                        if session['state']['started']:
                            await ws.send_str(json.dumps({
                                'type': 'game_start',
                                'state': session['state'],
                                'playerId': token,
                                'side': side
                            }))
                        else:
                            await ws.send_str(json.dumps({
                                'type': 'session_created',
                                'code': code,
                                'playerId': token,
                                'side': side
                            }))
                        print(f"[JOIN] {token[:8]} rejoined {code} (started={session['state']['started']})", flush=True)
                        continue
                    
                    if len(session['clients']) >= 2:
                        await ws.send_str(json.dumps({'type': 'error', 'message': 'Session is full'}))
                        continue
                    
                    state = session['state']
                    state['players'][token] = create_player(token, 'axis')
                    session['clients'].append(token)
                    client_sessions[token] = code
                    client_sides[token] = 'axis'
                    state['started'] = True
                    # Send game_start to ALL clients (use fresh WS lookups)
                    for ct in session['clients']:
                        target_ws = client_ws.get(ct)
                        if target_ws and not target_ws.closed:
                            try:
                                await target_ws.send_str(json.dumps({
                                    'type': 'game_start',
                                    'state': state,
                                    'playerId': ct,
                                    'side': state['players'][ct]['side']
                                }))
                                print(f"[START] Sent game_start to {ct[:8]}", flush=True)
                            except Exception as e:
                                print(f"[START] Failed to send to {ct[:8]}: {e}", flush=True)
                        else:
                            print(f"[START] {ct[:8]} WS not available", flush=True)
                    if code not in game_loops:
                        task = asyncio.ensure_future(game_loop(code))
                        game_loops[code] = task
                    print(f"[JOIN] {token[:8]} joined {code} — game starting!", flush=True)

                elif msg_type == 'find_match':
                    found = False
                    for fcode, fsession in sessions.items():
                        if len(fsession['clients']) == 1 and not fsession['state']['started']:
                            await ws.send_str(json.dumps({'type': 'match_found', 'code': fcode}))
                            found = True
                            break
                    if not found:
                        code = gen_code()
                        state = create_game_state()
                        state['players'][token] = create_player(token, 'allies')
                        sessions[code] = {'state': state, 'clients': [token], 'created': time.time()}
                        client_sessions[token] = code
                        client_sides[token] = 'allies'
                        await ws.send_str(json.dumps({
                            'type': 'waiting_match',
                            'code': code,
                            'playerId': token,
                            'side': 'allies'
                        }))

                elif msg_type == 'rejoin':
                    # Client reconnected and wants to rejoin their session
                    code = client_sessions.get(token)
                    if code and code in sessions:
                        session = sessions[code]
                        side = client_sides.get(token, 'allies')
                        if session['state']['started']:
                            await ws.send_str(json.dumps({
                                'type': 'game_start',
                                'state': session['state'],
                                'playerId': token,
                                'side': side
                            }))
                        else:
                            await ws.send_str(json.dumps({
                                'type': 'session_created',
                                'code': code,
                                'playerId': token,
                                'side': side
                            }))
                        print(f"[REJOIN] {token[:8]} rejoined {code}", flush=True)
                    else:
                        await ws.send_str(json.dumps({'type': 'rejoin_failed'}))
                        print(f"[REJOIN] {token[:8]} — no session found", flush=True)

                elif msg_type == 'input':
                    code = client_sessions.get(token)
                    if not code: continue
                    session = sessions.get(code)
                    if not session: continue
                    p = session['state']['players'].get(token)
                    if not p or not p['alive']: continue
                    inp = msg.get('input', {})
                    speed = 1.5 if p['crouching'] else 3
                    if inp.get('left'): p['vx'] = -speed; p['facing'] = 'left'
                    elif inp.get('right'): p['vx'] = speed; p['facing'] = 'right'
                    else: p['vx'] = 0
                    if inp.get('jump') and p['y'] >= 390: p['vy'] = -8
                    p['crouching'] = bool(inp.get('crouch'))
                    now = time.time() * 1000
                    if inp.get('shoot'):
                        fire_rates = {'rifle': 600, 'smg': 150, 'sniper': 800}
                        rate = fire_rates.get(p['weapon'], 600)
                        if now - p['lastShot'] > rate and p['ammo'] > 0 and not p['reloading']:
                            p['lastShot'] = now; p['ammo'] -= 1
                            d = 1 if p['facing'] == 'right' else -1
                            spread = (random.random()-0.5) * {'rifle':0.5,'smg':3,'sniper':0.1}.get(p['weapon'],0.5)
                            dmg = {'rifle':35,'smg':12,'sniper':70}.get(p['weapon'],35)
                            session['state']['projectiles'].append({
                                'x': p['x']+d*10, 'y': p['y']-(5 if p['crouching'] else 12),
                                'vx': 12*d, 'vy': spread, 'owner': token, 'damage': dmg,
                            })
                    if inp.get('grenade') and p['grenades'] > 0:
                        p['grenades'] -= 1; d = 1 if p['facing']=='right' else -1
                        session['state']['grenades'].append({
                            'x':p['x']+d*10,'y':p['y']-20,'vx':5*d,'vy':-6,'owner':token,'timer':120,'exploded':False
                        })
                    if inp.get('reload') and p['ammo'] < 30 and not p['reloading']:
                        p['reloading'] = True; p['reloadStart'] = now
                    if inp.get('weapon'): p['weapon'] = inp['weapon']
    
    except Exception as e:
        print(f"[WS ERROR] {e}", flush=True)
    
    finally:
        # DON'T delete the session or player data on disconnect!
        # The session persists so the client can rejoin.
        # Just clear the ws reference.
        if client_token:
            print(f"[WS] {client_token[:8]} disconnected", flush=True)
            if client_token in client_ws and client_ws[client_token] is ws:
                del client_ws[client_token]
    
    return ws

# ============ APP SETUP ============
async def index_handler(request):
    return web.FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'public', 'index.html'))

async def health_handler(request):
    session_details = {}
    for code, session in sessions.items():
        connected = []
        disconnected = []
        for ct in session['clients']:
            ws = client_ws.get(ct)
            if ws and not ws.closed:
                connected.append(ct[:8])
            else:
                disconnected.append(ct[:8])
        session_details[code] = {
            'clients': len(session['clients']),
            'connected': connected,
            'disconnected': disconnected,
            'started': session['state']['started'],
            'age': int(time.time() - session.get('created', 0)),
        }
    return web.json_response({
        'status': 'ok',
        'sessions': len(sessions),
        'details': session_details,
    })

# Periodic cleanup of very old abandoned sessions (>10 min with 0 connected)
async def periodic_cleanup(app):
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for code in list(sessions.keys()):
            session = sessions[code]
            age = now - session.get('created', 0)
            has_connected = any(
                client_ws.get(ct) and not client_ws[ct].closed 
                for ct in session['clients']
            )
            if age > 600 and not has_connected:
                for ct in session['clients']:
                    client_sessions.pop(ct, None)
                    client_sides.pop(ct, None)
                    client_ws.pop(ct, None)
                if code in game_loops:
                    game_loops[code].cancel()
                    del game_loops[code]
                del sessions[code]
                print(f"[CLEANUP] Removed abandoned session {code} (age: {age:.0f}s)", flush=True)

async def start_bg(app):
    app['cleanup'] = asyncio.ensure_future(periodic_cleanup(app))
async def stop_bg(app):
    app['cleanup'].cancel()

app = web.Application()
app.on_startup.append(start_bg)
app.on_cleanup.append(stop_bg)
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
