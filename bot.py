#!/usr/bin/env python3
"""Gall Game — Telegram Mini App, all-in-one, fully featured."""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import os
import random
import time
from urllib.parse import parse_qsl

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                           Message, WebAppInfo)
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy import (BigInteger, Column, DateTime, Float, ForeignKey,
                        Integer, String, select, desc)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base, relationship

# ══════════════════════════════════════════════════════════════ CONFIG
class Config:
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-me")
    DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./gall.db")
    WEBAPP_URL = os.environ.get("WEBAPP_URL", "http://localhost:8000")
    HOST = os.environ.get("HOST", "0.0.0.0")
    PORT = int(os.environ.get("PORT", "8000"))
    ENV = os.environ.get("ENV", "development")

    @classmethod
    def validate(cls):
        if not cls.BOT_TOKEN:
            raise RuntimeError("BOT_TOKEN is required")
        if cls.ENV == "production":
            if cls.JWT_SECRET == "dev-secret-change-me":
                raise RuntimeError("JWT_SECRET must be set in production")
            if not cls.WEBAPP_URL.startswith("https://"):
                raise RuntimeError("WEBAPP_URL must be https in production")


config = Config()
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("gall")

# ══════════════════════════════════════════════════════════════ MODELS
Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=False)
    username = Column(String(64))
    first_name = Column(String(128), default="")
    level = Column(Integer, default=1)
    xp = Column(Integer, default=0)
    coins = Column(Integer, default=50)
    room_id = Column(String(64), default="island-main")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.datetime.utcnow)
    player = relationship("PlayerState", back_populates="user",
                          uselist=False, cascade="all, delete-orphan")


class PlayerState(Base):
    __tablename__ = "player_states"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    location = Column(String(16), default="beach")     # ← NEW: current zone
    health = Column(Float, default=100.0)
    max_health = Column(Float, default=100.0)
    energy = Column(Float, default=100.0)
    max_energy = Column(Float, default=100.0)
    hunger = Column(Float, default=100.0)
    water = Column(Float, default=100.0)
    wood = Column(Integer, default=0)
    stone = Column(Integer, default=0)
    food = Column(Integer, default=0)
    tools = Column(String(256), default="")
    house_level = Column(Integer, default=0)            # ← NEW: shelter tier
    last_gather = Column(Float, default=0.0)
    last_pvp = Column(Float, default=0.0)
    last_move = Column(Float, default=0.0)              # ← NEW
    last_update = Column(DateTime, default=datetime.datetime.utcnow)
    user = relationship("User", back_populates="player")

# ══════════════════════════════════════════════════════════════ DATABASE
engine = create_async_engine(config.DATABASE_URL, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# ══════════════════════════════════════════════════════════════ AUTH
def verify_init_data(init_data: str) -> dict | None:
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=False))
        received = parsed.pop("hash", None)
        parsed.pop("signature", None)
        if not received:
            return None
        check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            return None
        auth_date = int(parsed.get("auth_date", "0"))
        if time.time() - auth_date > 86400:
            return None
        return json.loads(parsed.get("user", "{}"))
    except Exception:
        return None


def make_token(telegram_id: int) -> str:
    return jwt.encode(
        {"sub": str(telegram_id), "exp": int(time.time()) + 86400},
        config.JWT_SECRET, algorithm="HS256",
    )


def decode_token(token: str) -> int | None:
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
        return int(payload["sub"])
    except JWTError:
        return None

# ══════════════════════════════════════════════════════════════ WORLD MAP
# Island zones with resources and connections
ZONES = {
    "beach": {
        "name": "ساحل", "emoji": "🏖",
        "desc": "ساحل آرام با شن و نارگیل",
        "mult": {"wood": 0.5, "stone": 0.2, "food": 1.5, "water": 0.3},
        "neighbors": ["forest", "river"],
        "x": 0, "y": 1,
    },
    "forest": {
        "name": "جنگل", "emoji": "🌲",
        "desc": "جنگل انبوه، منبع چوب",
        "mult": {"wood": 2.0, "stone": 0.3, "food": 1.2, "water": 0.2},
        "neighbors": ["beach", "river", "cave"],
        "x": 1, "y": 0,
    },
    "river": {
        "name": "رودخانه", "emoji": "🏞",
        "desc": "آب شیرین و ماهی",
        "mult": {"wood": 0.5, "stone": 0.5, "food": 1.3, "water": 2.5},
        "neighbors": ["beach", "forest", "mountain"],
        "x": 1, "y": 2,
    },
    "cave": {
        "name": "غار", "emoji": "🕳",
        "desc": "سنگ و کانی در اعماق",
        "mult": {"wood": 0.1, "stone": 2.2, "food": 0.1, "water": 0.1},
        "neighbors": ["forest", "mountain"],
        "x": 2, "y": 0,
    },
    "mountain": {
        "name": "کوه", "emoji": "⛰",
        "desc": "ارتفاعات، سنگ و بلور",
        "mult": {"wood": 0.3, "stone": 1.8, "food": 0.2, "water": 0.1},
        "neighbors": ["river", "cave"],
        "x": 2, "y": 2,
    },
}

MOVE_COOLDOWN = 1.5
MOVE_ENERGY_COST = 3.0

# ══════════════════════════════════════════════════════════════ GAME ENGINE
GATHER = {
    "wood":  {"energy": 5, "yield": (2, 5), "cd": 2.0, "xp": 3, "hunger": 1},
    "stone": {"energy": 8, "yield": (1, 4), "cd": 3.5, "xp": 4, "hunger": 2},
    "food":  {"energy": 3, "yield": (1, 3), "cd": 2.0, "xp": 2, "hunger": 1},
    "water": {"energy": 2, "yield": (2, 4), "cd": 1.5, "xp": 1, "hunger": 0},
}

RECIPES = {
    "axe":         {"wood": 5,  "stone": 3,  "coin": 5,  "xp": 10},
    "pickaxe":     {"wood": 8,  "stone": 6,  "coin": 8,  "xp": 15},
    "spear":       {"wood": 10, "stone": 4,  "coin": 12, "xp": 20},
    "shelter":     {"wood": 30, "stone": 20, "coin": 25, "xp": 40},
    "cooked_food": {"food": 2,  "wood": 1,   "coin": 2,  "xp": 5},
}


class ActionError(Exception):
    pass


def xp_for_level(lvl: int) -> int:
    return 50 * lvl * lvl


def _tools_set(state) -> set:
    return set(t for t in (state.tools or "").split(",") if t)


def apply_tick(state, dt: float):
    if dt <= 0:
        return
    state.hunger = max(0.0, state.hunger - dt * 0.10)
    state.water = max(0.0, state.water - dt * 0.15)
    if state.hunger <= 0 or state.water <= 0:
        dmg = 0.0
        if state.hunger <= 0:
            dmg += dt * 1.5
        if state.water <= 0:
            dmg += dt * 1.5
        state.health = max(0.0, state.health - dmg)
    else:
        state.energy = min(state.max_energy, state.energy + dt * 0.8)
        if state.hunger > 40 and state.water > 40:
            state.health = min(state.max_health, state.health + dt * 0.3)
    if state.health <= 0:
        state.health = state.max_health * 0.5
        state.hunger = 50.0
        state.water = 50.0
        state.energy = 50.0
        state.location = "beach"
        state.wood = int(state.wood * 0.7)
        state.stone = int(state.stone * 0.7)
        state.food = int(state.food * 0.7)


def move_zone(state, target: str, now: float) -> dict:
    if target not in ZONES:
        raise ActionError("منطقه نامعتبر")
    current = state.location or "beach"
    if target == current:
        raise ActionError("همین‌جا هستی")
    if target not in ZONES[current]["neighbors"]:
        raise ActionError(f"از {ZONES[current]['name']} نمی‌توانی مستقیم به {ZONES[target]['name']} بروی")
    if now - state.last_move < MOVE_COOLDOWN:
        raise ActionError("کمی صبر کن")
    if state.energy < MOVE_ENERGY_COST:
        raise ActionError("انرژی کافی نداری")
    state.location = target
    state.energy -= MOVE_ENERGY_COST
    state.last_move = now
    return {"moved_to": target, "zone_name": ZONES[target]["name"], "xp": 1}


def gather(state, resource: str, now: float) -> dict:
    if resource not in GATHER:
        raise ActionError("منبع نامعتبر")
    cfg = GATHER[resource]
    if now - state.last_gather < cfg["cd"]:
        raise ActionError("کمی صبر کن")
    if state.energy < cfg["energy"]:
        raise ActionError("انرژی کافی نداری")
    if state.hunger <= 0 or state.water <= 0:
        raise ActionError("اول غذا/آب بخور")

    zone = state.location or "beach"
    mult = ZONES[zone]["mult"].get(resource, 0)
    if mult == 0:
        raise ActionError(f"در {ZONES[zone]['name']} این منبع یافت نمی‌شود")

    tools = _tools_set(state)
    tool_bonus = 1.0
    if resource == "wood" and "axe" in tools:
        tool_bonus = 1.7
    if resource == "stone" and "pickaxe" in tools:
        tool_bonus = 1.7
    if resource == "food" and "spear" in tools:
        tool_bonus = 1.5

    raw = random.randint(*cfg["yield"])
    amount = max(1, int(raw * mult * tool_bonus))

    if resource == "wood":
        state.wood += amount
    elif resource == "stone":
        state.stone += amount
    elif resource == "food":
        state.food += amount
    elif resource == "water":
        state.water = min(100.0, state.water + amount * 8)
        amount = int(amount * 8)

    state.energy = max(0.0, state.energy - cfg["energy"])
    state.hunger = max(0.0, state.hunger - cfg["hunger"])
    state.last_gather = now
    return {"resource": resource, "amount": amount, "xp": cfg["xp"]}


def craft(state, recipe: str) -> dict:
    if recipe not in RECIPES:
        raise ActionError("دستور نامعتبر")
    r = RECIPES[recipe]
    tools = _tools_set(state)
    if recipe in tools:
        raise ActionError("قبلاً ساخته شده")
    if state.wood < r.get("wood", 0):
        raise ActionError("چوب کافی نداری")
    if state.stone < r.get("stone", 0):
        raise ActionError("سنگ کافی نداری")
    if state.food < r.get("food", 0):
        raise ActionError("غذا کافی نداری")
    state.wood -= r.get("wood", 0)
    state.stone -= r.get("stone", 0)
    state.food -= r.get("food", 0)
    if recipe == "shelter":
        state.max_health = 150.0
        state.health = min(state.max_health, state.health + 50)
        state.house_level = max(state.house_level, 1)
    elif recipe == "cooked_food":
        state.food += 4
        state.hunger = min(100.0, state.hunger + 30)
        state.water = min(100.0, state.water + 10)
    else:
        tools.add(recipe)
        state.tools = ",".join(tools)
    return {"recipe": recipe, "xp": r["xp"], "coin_cost": r["coin"]}


def eat(state, item: str) -> dict:
    if item == "food":
        if state.food < 1:
            raise ActionError("غذا نداری")
        state.food -= 1
        state.hunger = min(100.0, state.hunger + 20)
        return {"ate": "food", "xp": 1}
    raise ActionError("نامعتبر")


def rest(state) -> dict:
    if state.energy >= state.max_energy:
        raise ActionError("پر انرژی هستی")
    state.energy = min(state.max_energy, state.energy + 40)
    state.hunger = max(0.0, state.hunger - 5)
    return {"rest": True, "xp": 1}


def attack(attacker, target, attacker_level: int, now: float) -> dict:
    if now - attacker.last_pvp < 15:
        raise ActionError("کمی صبر کن")
    if attacker.energy < 20:
        raise ActionError("انرژی کافی نداری")
    if attacker.location != target.location:
        raise ActionError("باید در همان منطقه باشید")
    tools = _tools_set(attacker)
    dmg = 15 + attacker_level * 2
    if "spear" in tools:
        dmg = int(dmg * 1.5)
    was_alive = target.health > 0
    target.health = max(0.0, target.health - dmg)
    attacker.energy = max(0.0, attacker.energy - 20)
    attacker.last_pvp = now
    stolen = {"wood": 0, "stone": 0, "food": 0}
    killed = was_alive and target.health <= 0
    if killed:
        for res in stolen:
            amt = int(getattr(target, res) * 0.15)
            if amt > 0:
                setattr(target, res, getattr(target, res) - amt)
                setattr(attacker, res, getattr(attacker, res) + amt)
                stolen[res] = amt
        target.health = target.max_health * 0.5
        target.energy = target.max_energy * 0.5
    return {"damage": dmg, "stolen": stolen, "killed": killed,
            "xp": 8 if killed else 3}

# ══════════════════════════════════════════════════════════════ HTML
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Gall Game</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root{--bg:#0a1628;--card:#122036;--card2:#1a2b44;--line:#22395c;--text:#e8f0ff;
    --muted:#7a8ea8;--accent:#ffb84d;--ok:#4ade80;--danger:#ef4444;
    --water:#38bdf8;--energy:#fbbf24;--hunger:#f97316;--health:#ef4444}
  *{box-sizing:border-box}
  html,body{margin:0;padding:0;background:var(--bg);color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Tahoma,sans-serif;
    -webkit-tap-highlight-color:transparent;overscroll-behavior:none}
  body{padding:10px 10px 90px}
  h3{margin:14px 4px 8px;font-size:14px;color:var(--muted);font-weight:600}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;margin-bottom:10px}
  .profile{display:flex;gap:10px;align-items:center}
  .avatar{width:42px;height:42px;border-radius:50%;background:linear-gradient(135deg,#1e90ff,#0f4c81);
    display:grid;place-items:center;font-size:20px}
  .name{font-weight:700;font-size:15px}
  .sub{font-size:12px;color:var(--muted);margin-top:2px}
  .xp{width:120px;height:4px;background:#0a1628;border-radius:3px;margin-top:4px;overflow:hidden}
  .xp-fill{height:100%;width:0;background:var(--accent);transition:width .3s}
  .stats{display:grid;gap:6px;margin-top:10px}
  .stat{display:flex;align-items:center;gap:8px;font-size:12px}
  .stat-label{width:70px;color:var(--muted)}
  .bar{flex:1;height:8px;background:#0a1628;border-radius:5px;overflow:hidden}
  .fill{height:100%;width:100%;border-radius:5px;transition:width .35s}
  .fill.health{background:var(--health)}.fill.energy{background:var(--energy)}
  .fill.hunger{background:var(--hunger)}.fill.water{background:var(--water)}
  .val{width:34px;text-align:left;font-variant-numeric:tabular-nums}
  .resources{display:flex;gap:8px;margin-top:12px}
  .res{flex:1;background:var(--card2);border-radius:10px;padding:8px;text-align:center;font-size:13px}
  .res b{display:block;font-size:16px;color:var(--accent)}

  /* MAP */
  .map-wrap{position:relative;background:linear-gradient(180deg,#0d3a5c,#0a2842);
    border-radius:14px;padding:14px 8px;min-height:200px}
  .map-grid{display:grid;grid-template-columns:repeat(3,1fr);grid-template-rows:repeat(3,1fr);
    gap:8px;max-width:320px;margin:0 auto}
  .zone{background:var(--card2);border:2px solid var(--line);border-radius:12px;
    padding:10px 4px;text-align:center;cursor:pointer;transition:.2s;position:relative;min-height:70px}
  .zone:hover{border-color:var(--accent)}
  .zone.current{border-color:var(--ok);background:#0e3322}
  .zone.neighbor{border-color:#2b6ca3;background:#122c44}
  .zone.locked{opacity:.4;cursor:not-allowed}
  .zone-emoji{font-size:22px;line-height:1}
  .zone-name{font-size:11px;margin-top:4px;font-weight:600}
  .zone-players{font-size:10px;color:var(--accent);margin-top:2px;min-height:12px}
  .zone-badge{position:absolute;top:4px;left:4px;background:var(--ok);
    border-radius:50%;width:8px;height:8px}
  .zone-info{margin-top:10px;text-align:center;font-size:12px;color:var(--muted)}

  .actions{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
  button{background:var(--card2);color:var(--text);border:1px solid var(--line);
    border-radius:12px;padding:12px 6px;font-size:13px;font-weight:600;cursor:pointer;
    transition:transform .08s,background .2s;font-family:inherit}
  button:active{transform:scale(.96);background:#24406a}
  button:disabled{opacity:.5;cursor:not-allowed}
  .btn-attack{background:#3b1220;border-color:#5e1c2f;color:#ff9a9a;padding:8px 12px;font-size:12px}
  .craft{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}
  .craft button{padding:10px;font-size:12px}

  .player{display:flex;justify-content:space-between;align-items:center;
    padding:8px 10px;background:var(--card2);border-radius:10px;margin-bottom:6px}
  .pname{font-size:13px;font-weight:600}
  .pmeta{font-size:11px;color:var(--muted);margin-top:2px}
  .muted{color:var(--muted);font-size:12px}
  .tool{display:inline-block;background:var(--card2);border-radius:8px;
    padding:4px 8px;margin:0 4px 4px 0;font-size:12px}
  .lb-row{display:flex;justify-content:space-between;padding:8px;
    background:var(--card2);border-radius:10px;margin-bottom:4px;font-size:13px}
  .lb-rank{color:var(--accent);font-weight:700;width:24px}
  .lb-me{background:#0e3322;border:1px solid var(--ok)}

  #chat-log{max-height:150px;overflow-y:auto;background:#0a1628;border-radius:10px;
    padding:8px;font-size:12px;line-height:1.6;margin-bottom:8px}
  .chat-msg{margin-bottom:4px;word-wrap:break-word}
  .chat-input{display:flex;gap:6px}
  .chat-input input{flex:1;background:#0a1628;border:1px solid var(--line);
    border-radius:10px;padding:10px;color:var(--text);font-family:inherit;font-size:13px}
  .chat-input button{padding:10px 16px}
  #toasts{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);
    display:flex;flex-direction:column;gap:6px;z-index:999;pointer-events:none;
    width:88%;max-width:360px}
  .toast{background:var(--card2);border:1px solid var(--line);border-radius:10px;
    padding:8px 12px;font-size:12px;text-align:center;animation:slide .25s ease-out}
  .toast.success{border-color:var(--ok);color:var(--ok)}
  .toast.danger{border-color:var(--danger);color:var(--danger)}
  .toast.warn{border-color:var(--accent);color:var(--accent)}
  @keyframes slide{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
</style>
</head>
<body>

<div class="card profile">
  <div class="avatar">🏝</div>
  <div>
    <div class="name" id="name">در حال اتصال…</div>
    <div class="sub">سطح <span id="level">1</span> · <span id="coins">0</span> 🪙</div>
    <div class="xp"><div class="xp-fill" id="xp-bar"></div></div>
  </div>
</div>

<div class="card">
  <div class="stats">
    <div class="stat"><span class="stat-label">❤ سلامتی</span><div class="bar"><div class="fill health" id="bar-health"></div></div><span class="val" id="val-health">0</span></div>
    <div class="stat"><span class="stat-label">⚡ انرژی</span><div class="bar"><div class="fill energy" id="bar-energy"></div></div><span class="val" id="val-energy">0</span></div>
    <div class="stat"><span class="stat-label">🍖 گرسنگی</span><div class="bar"><div class="fill hunger" id="bar-hunger"></div></div><span class="val" id="val-hunger">0</span></div>
    <div class="stat"><span class="stat-label">💧 آب</span><div class="bar"><div class="fill water" id="bar-water"></div></div><span class="val" id="val-water">0</span></div>
  </div>
  <div class="resources">
    <div class="res">🪵<b id="wood">0</b></div>
    <div class="res">🪨<b id="stone">0</b></div>
    <div class="res">🍎<b id="food">0</b></div>
  </div>
</div>

<h3>🗺 نقشه جزیره</h3>
<div class="map-wrap">
  <div class="map-grid" id="map-grid"></div>
  <div class="zone-info" id="zone-info">—</div>
</div>

<h3>🌲 جمع‌آوری (در منطقه فعلی)</h3>
<div class="actions">
  <button onclick="act('gather',{resource:'wood'})">🪓 چوب</button>
  <button onclick="act('gather',{resource:'stone'})">⛏ سنگ</button>
  <button onclick="act('gather',{resource:'food'})">🍎 غذا</button>
  <button onclick="act('gather',{resource:'water'})">💧 آب</button>
  <button onclick="act('rest')">😴 استراحت</button>
  <button onclick="act('eat',{item:'food'})">🍽 خوردن</button>
</div>

<h3>🔨 ساخت</h3>
<div class="craft">
  <button onclick="act('craft',{recipe:'axe'})">🪓 تبر<br><span class="muted">5🪵 3🪨 5🪙</span></button>
  <button onclick="act('craft',{recipe:'pickaxe'})">⛏ کلنگ<br><span class="muted">8🪵 6🪨 8🪙</span></button>
  <button onclick="act('craft',{recipe:'spear'})">🗡 نیزه<br><span class="muted">10🪵 4🪨 12🪙</span></button>
  <button onclick="act('craft',{recipe:'shelter'})">⛺ پناهگاه<br><span class="muted">30🪵 20🪨 25🪙</span></button>
  <button onclick="act('craft',{recipe:'cooked_food'})">🍲 غذای پخته<br><span class="muted">2🍎 1🪵 2🪙</span></button>
</div>

<h3>🔧 ابزارها</h3>
<div class="card" id="tools"><span class="muted">—</span></div>

<h3>👥 بازیکنان (همه مناطق)</h3>
<div id="players"></div>

<h3>🏆 رتبه‌بندی</h3>
<div id="leaderboard"><div class="muted">در حال بارگذاری…</div></div>

<h3>💬 چت</h3>
<div id="chat-log"></div>
<div class="chat-input">
  <input id="chat-input" placeholder="پیام…" maxlength="200">
  <button onclick="sendChat()">ارسال</button>
</div>

<div id="toasts"></div>

<script>
const tg = window.Telegram && window.Telegram.WebApp;
if(tg){try{tg.ready();tg.expand();tg.setHeaderColor('#0a1628')}catch(e){}}

let token=null, ws=null, state=null, players=[], myId=null, zones={};
const roomId = new URLSearchParams(location.search).get('room') || 'island-main';

function toast(msg,kind='info'){
  const t=document.createElement('div');
  t.className='toast '+kind;t.textContent=msg;
  document.getElementById('toasts').appendChild(t);
  setTimeout(()=>t.remove(),2500);
}

async function login(){
  if(!tg||!tg.initData){toast('فقط از داخل تلگرام باز کن','warn');return;}
  try{
    const res=await fetch('/api/login',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({init_data:tg.initData,room_id:roomId})});
    if(!res.ok){toast('ورود ناموفق','danger');return;}
    const d=await res.json();
    token=d.token;myId=d.user_id;
    connect();loadLeaderboard();
    setInterval(loadLeaderboard,15000);
  }catch(e){toast('خطای شبکه','danger');setTimeout(login,3000);}
}

function connect(){
  const proto=location.protocol==='https:'?'wss:':'ws:';
  ws=new WebSocket(proto+'//'+location.host+'/ws?token='+encodeURIComponent(token));
  ws.onopen=()=>toast('به جزیره وصل شدی 🏝','success');
  ws.onclose=()=>{toast('قطع ارتباط… تلاش مجدد','warn');setTimeout(connect,2500);};
  ws.onerror=()=>ws.close();
  ws.onmessage=ev=>{try{handle(JSON.parse(ev.data));}catch(e){}};
}

function handle(m){
  if(m.type==='state'){state=m.data;if(m.action)flash(m.action);renderSelf();}
  else if(m.type==='players'){players=m.data;renderPlayers();renderMap();}
  else if(m.type==='event'){showEvent(m.data);}
  else if(m.type==='chat'){addChat(m.data);}
  else if(m.type==='error'){toast(m.data,'warn');}
}

function act(action,extra={}){
  if(!ws||ws.readyState!==1){toast('در حال اتصال…','warn');return;}
  ws.send(JSON.stringify(Object.assign({type:'action',action},extra)));
}

function sendChat(){
  const el=document.getElementById('chat-input');
  const t=el.value.trim();if(!t||!ws)return;
  ws.send(JSON.stringify({type:'chat',text:t}));
  el.value='';
}
document.getElementById('chat-input').addEventListener('keydown',e=>{if(e.key==='Enter')sendChat();});

function bar(id,v,max){
  const pct=Math.max(0,Math.min(100,v/max*100));
  document.getElementById('bar-'+id).style.width=pct+'%';
  document.getElementById('val-'+id).textContent=Math.round(v);
}

function renderSelf(){
  if(!state)return;
  document.getElementById('name').textContent=state.username;
  document.getElementById('level').textContent=state.level;
  document.getElementById('coins').textContent=state.coins;
  document.getElementById('xp-bar').style.width=(state.xp/state.xp_next*100)+'%';
  bar('health',state.health,state.max_health);
  bar('energy',state.energy,state.max_energy);
  bar('hunger',state.hunger,100);
  bar('water',state.water,100);
  document.getElementById('wood').textContent=state.wood;
  document.getElementById('stone').textContent=state.stone;
  document.getElementById('food').textContent=state.food;
  const tools=state.tools||[];
  document.getElementById('tools').innerHTML=tools.length
    ?tools.map(t=>`<span class="tool">${ti(t)} ${tn(t)}</span>`).join('')
    :'<span class="muted">هیچ ابزاری نداری</span>';
  renderMap();
}

function ti(t){return{axe:'🪓',pickaxe:'⛏',spear:'🗡',shelter:'⛺'}[t]||'🔧';}
function tn(t){return{axe:'تبر',pickaxe:'کلنگ',spear:'نیزه',shelter:'پناهگاه'}[t]||t;}

function renderMap(){
  if(!state||!state.zones)return;
  zones=state.zones;
  const me=state.location;
  const neighbors=(zones[me]||{}).neighbors||[];
  const grid=document.getElementById('map-grid');
  const cells=[];
  // 3x3 layout: [beach _, forest _ cave] top
  //             [_,_,_]
  //             [river _ mountain] bottom
  const layout=[
    [null,'forest','cave'],
    ['beach',null,null],
    [null,'river','mountain'],
  ];
  for(const row of layout){
    for(const key of row){
      if(!key){cells.push('<div></div>');continue;}
      const z=zones[key];
      const isCurrent=key===me;
      const isNeighbor=neighbors.includes(key);
      const cls='zone'+(isCurrent?' current':isNeighbor?' neighbor':' locked');
      const hereCount=(players.filter(p=>p.zone===key)).length;
      cells.push(`<div class="${cls}" onclick="${isNeighbor?`act('move',{target:'${key}'})`:''}">
        <div class="zone-emoji">${z.emoji}</div>
        <div class="zone-name">${z.name}</div>
        <div class="zone-players">${hereCount?'👤'+hereCount:''}</div>
        ${isCurrent?'<div class="zone-badge"></div>':''}
      </div>`);
    }
  }
  grid.innerHTML=cells.join('');
  const cur=zones[me]||{};
  document.getElementById('zone-info').textContent=
    `📍 ${cur.emoji||''} ${cur.name||''} — ${cur.desc||''}`;
}

function renderPlayers(){
  const el=document.getElementById('players');
  if(!players.length){el.innerHTML='<div class="card muted">تنها هستی</div>';return;}
  el.innerHTML=players.map(p=>{
    const isMe=p.id===myId;
    const zoneName=(state&&state.zones&&state.zones[p.zone])?state.zones[p.zone].name:p.zone||'?';
    return `<div class="player">
      <div>
        <div class="pname">${isMe?'🟢 ':''}${esc(p.name)}</div>
        <div class="pmeta">Lv ${p.level} · ❤ ${Math.round(p.health)} · ${zoneName}</div>
      </div>
      ${isMe?'':'<button class="btn-attack" onclick="act(\'attack\',{target:'+p.id+'})">⚔ حمله</button>'}
    </div>`;
  }).join('');
}

async function loadLeaderboard(){
  try{
    const res=await fetch('/api/leaderboard?room='+encodeURIComponent(roomId));
    if(!res.ok)return;
    const d=await res.json();
    const el=document.getElementById('leaderboard');
    if(!d.players||!d.players.length){el.innerHTML='<div class="muted">خالی</div>';return;}
    el.innerHTML=d.players.map((p,i)=>`
      <div class="lb-row ${p.id===myId?'lb-me':''}">
        <span class="lb-rank">${['🥇','🥈','🥉'][i]||(i+1)}</span>
        <span>${esc(p.name)}</span>
        <span class="muted">Lv ${p.level} · ${p.xp} XP</span>
      </div>`).join('');
  }catch(e){}
}

function showEvent(e){
  if(e.kind==='attack')toast(`${e.attacker} به ${e.target} حمله کرد (-${e.damage})`,'danger');
  else if(e.kind==='levelup')toast(`🎉 ${e.who} به سطح ${e.level} رسید`,'success');
}

function flash(a){
  if(a.moved_to)toast(`به ${a.zone_name} رفتی`,'success');
  else if(a.resource)toast(`+${a.amount} ${rn(a.resource)}`);
  else if(a.recipe)toast(`ساخته شد: ${tn(a.recipe)}`,'success');
  else if(a.killed)toast('دشمن کشته شد! 🏆','success');
  else if(a.damage)toast(`ضربه زدی (-${a.damage})`,'danger');
}

function rn(r){return{wood:'چوب',stone:'سنگ',food:'غذا',water:'آب'}[r]||r;}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

function addChat(m){
  const box=document.getElementById('chat-log');
  const div=document.createElement('div');
  div.className='chat-msg';
  const name=(players.find(p=>p.id===m.from)||{}).name||'ناشناس';
  div.innerHTML=`<b>${esc(name)}:</b> ${esc(m.text)}`;
  box.appendChild(div);
  box.scrollTop=box.scrollHeight;
  while(box.children.length>60)box.firstChild.remove();
}

login();
</script>
</body>
</html>
"""

# ══════════════════════════════════════════════════════════════ SERVER
app = FastAPI()
rooms: dict[str, dict[int, "Connection"]] = {}


class Connection:
    def __init__(self, ws: WebSocket, user_id: int, room_id: str):
        self.ws = ws
        self.user_id = user_id
        self.room_id = room_id
        self.actions: list[float] = []


def serialize_self(user: User, ps: PlayerState) -> dict:
    return {
        "id": user.id,
        "username": user.username or user.first_name or f"Player{user.id}",
        "level": user.level,
        "xp": user.xp,
        "xp_next": xp_for_level(user.level),
        "coins": user.coins,
        "location": ps.location or "beach",
        "zones": ZONES,
        "health": round(ps.health, 1), "max_health": ps.max_health,
        "energy": round(ps.energy, 1), "max_energy": ps.max_energy,
        "hunger": round(ps.hunger, 1), "water": round(ps.water, 1),
        "wood": ps.wood, "stone": ps.stone, "food": ps.food,
        "tools": [t for t in (ps.tools or "").split(",") if t],
        "house_level": ps.house_level or 0,
    }


async def broadcast(room_id: str, msg: dict):
    conns = rooms.get(room_id, {})
    data = json.dumps(msg, ensure_ascii=False)
    dead = []
    for uid, c in list(conns.items()):
        try:
            await c.ws.send_text(data)
        except Exception:
            dead.append(uid)
    for d in dead:
        conns.pop(d, None)


async def send_state(conn: Connection):
    async with SessionLocal() as s:
        user = await s.get(User, conn.user_id)
        ps = (await s.execute(
            select(PlayerState).where(PlayerState.user_id == conn.user_id)
        )).scalar_one_or_none()
        if not user or not ps:
            return
        payload = serialize_self(user, ps)
    await conn.ws.send_text(json.dumps({"type": "state", "data": payload},
                                       ensure_ascii=False))


async def broadcast_presence(room_id: str):
    conns = rooms.get(room_id, {})
    if not conns:
        return
    players = []
    async with SessionLocal() as s:
        for uid in conns:
            u = await s.get(User, uid)
            if not u:
                continue
            ps = (await s.execute(
                select(PlayerState).where(PlayerState.user_id == uid)
            )).scalar_one_or_none()
            players.append({
                "id": u.id,
                "name": u.username or u.first_name or f"Player{u.id}",
                "level": u.level,
                "health": round(ps.health, 1) if ps else 0,
                "zone": (ps.location if ps else None) or "beach",
            })
    await broadcast(room_id, {"type": "players", "data": players})


async def do_action(conn: Connection, msg: dict):
    action = msg.get("action")
    now = time.time()
    now_dt = datetime.datetime.utcnow()

    async with SessionLocal() as s:
        user = await s.get(User, conn.user_id)
        ps = (await s.execute(
            select(PlayerState).where(PlayerState.user_id == conn.user_id)
        )).scalar_one()

        last = ps.last_update or now_dt
        delta = (now_dt - last).total_seconds()
        apply_tick(ps, max(0.0, min(delta, 60.0)))
        ps.last_update = now_dt

        result = None
        try:
            if action == "move":
                result = move_zone(ps, str(msg.get("target", "")), now)
            elif action == "gather":
                result = gather(ps, msg.get("resource", ""), now)
            elif action == "craft":
                r = craft(ps, msg.get("recipe", ""))
                if user.coins < r["coin_cost"]:
                    raise ActionError("سکه کافی نداری")
                user.coins -= r["coin_cost"]
                result = r
            elif action == "eat":
                result = eat(ps, msg.get("item", ""))
            elif action == "rest":
                result = rest(ps)
            elif action == "attack":
                target_uid = int(msg.get("target", 0))
                if target_uid == conn.user_id:
                    raise ActionError("خودت را نمی‌توانی بزنی")
                if target_uid not in rooms.get(conn.room_id, {}):
                    raise ActionError("هدف در دسترس نیست")
                target = await s.get(User, target_uid)
                target_ps = (await s.execute(
                    select(PlayerState).where(PlayerState.user_id == target_uid)
                )).scalar_one_or_none()
                if not target or not target_ps:
                    raise ActionError("هدف در دسترس نیست")
                tdelta = (now_dt - (target_ps.last_update or now_dt)).total_seconds()
                apply_tick(target_ps, max(0.0, min(tdelta, 60.0)))
                target_ps.last_update = now_dt
                result = attack(ps, target_ps, user.level, now)
                await broadcast(conn.room_id, {
                    "type": "event",
                    "data": {"kind": "attack",
                             "attacker": user.username or user.first_name,
                             "target": target.username or target.first_name,
                             "damage": result["damage"]},
                })
            else:
                raise ActionError("اکشن نامعتبر")

            xp = result.get("xp", 0)
            if xp:
                user.xp += xp
                while user.xp >= xp_for_level(user.level):
                    user.xp -= xp_for_level(user.level)
                    user.level += 1
                    user.coins += 10
                    await broadcast(conn.room_id, {
                        "type": "event",
                        "data": {"kind": "levelup",
                                 "who": user.username or user.first_name,
                                 "level": user.level},
                    })

            await s.commit()
            await s.refresh(user)
            await s.refresh(ps)
            await conn.ws.send_text(json.dumps({
                "type": "state", "data": serialize_self(user, ps),
                "action": result,
            }, ensure_ascii=False))
            await broadcast_presence(conn.room_id)
        except ActionError as e:
            await s.rollback()
            await conn.ws.send_text(json.dumps(
                {"type": "error", "data": str(e)}, ensure_ascii=False))


async def handle_message(conn: Connection, raw: str):
    try:
        msg = json.loads(raw)
    except Exception:
        return
    now = time.time()
    conn.actions = [t for t in conn.actions if now - t < 1.0]
    if len(conn.actions) >= 15:
        return
    conn.actions.append(now)
    t = msg.get("type")
    try:
        if t == "action":
            await do_action(conn, msg)
        elif t == "chat":
            text = str(msg.get("text", "")).strip()[:200]
            if not text:
                return
            await broadcast(conn.room_id, {
                "type": "chat",
                "data": {"from": conn.user_id, "text": text, "ts": now},
            })
    except Exception:
        log.exception("handle_message error")
        try:
            await conn.ws.send_text(json.dumps(
                {"type": "error", "data": "خطای سرور"}, ensure_ascii=False))
        except Exception:
            pass


async def game_tick_loop():
    while True:
        await asyncio.sleep(2)
        try:
            active = set()
            for conns in rooms.values():
                active.update(conns.keys())
            if not active:
                continue
            now_dt = datetime.datetime.utcnow()
            async with SessionLocal() as s:
                for uid in active:
                    ps = (await s.execute(
                        select(PlayerState).where(PlayerState.user_id == uid)
                    )).scalar_one_or_none()
                    if not ps:
                        continue
                    delta = (now_dt - (ps.last_update or now_dt)).total_seconds()
                    apply_tick(ps, max(0.0, min(delta, 10.0)))
                    ps.last_update = now_dt
                await s.commit()
            for room_id in list(rooms.keys()):
                if rooms.get(room_id):
                    await broadcast_presence(room_id)
        except Exception:
            log.exception("tick error")


class LoginReq(BaseModel):
    init_data: str
    room_id: str | None = None


@app.post("/api/login")
async def api_login(req: LoginReq):
    ud = verify_init_data(req.init_data)
    if not ud:
        raise HTTPException(401, "invalid initData")
    tg_id = int(ud["id"])
    async with SessionLocal() as s:
        user = (await s.execute(
            select(User).where(User.telegram_id == tg_id)
        )).scalar_one_or_none()
        if not user:
            user = User(telegram_id=tg_id,
                        username=ud.get("username"),
                        first_name=ud.get("first_name", ""))
            s.add(user)
            await s.flush()
            s.add(PlayerState(user_id=user.id, location="beach"))
        else:
            user.username = ud.get("username")
            user.first_name = ud.get("first_name", "")
            user.last_seen = datetime.datetime.utcnow()
        if req.room_id:
            user.room_id = req.room_id[:64]
        await s.commit()
        await s.refresh(user)
        return {"token": make_token(tg_id), "user_id": user.id,
                "room_id": user.room_id}


@app.get("/api/leaderboard")
async def api_leaderboard(room: str = "island-main"):
    async with SessionLocal() as s:
        rows = await s.execute(
            select(User).where(User.room_id == room[:64])
            .order_by(desc(User.level), desc(User.xp)).limit(20)
        )
        users = rows.scalars().all()
        return {"players": [{
            "id": u.id,
            "name": u.username or u.first_name or f"Player{u.id}",
            "level": u.level,
            "xp": u.xp,
        } for u in users]}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str = ""):
    tg_id = decode_token(token)
    if not tg_id:
        await ws.close(code=4001); return
    await ws.accept()
    async with SessionLocal() as s:
        user = (await s.execute(
            select(User).where(User.telegram_id == tg_id)
        )).scalar_one_or_none()
        if not user:
            await ws.close(code=4002); return
        conn = Connection(ws, user.id, user.room_id or "island-main")
        user.last_seen = datetime.datetime.utcnow()
        await s.commit()
    rooms.setdefault(conn.room_id, {})[conn.user_id] = conn
    await send_state(conn)
    await broadcast_presence(conn.room_id)
    try:
        while True:
            raw = await ws.receive_text()
            await handle_message(conn, raw)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws error")
    finally:
        rooms.get(conn.room_id, {}).pop(conn.user_id, None)
        try:
            await broadcast_presence(conn.room_id)
        except Exception:
            pass


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


@app.get("/health")
async def health():
    return {"ok": True}

# ══════════════════════════════════════════════════════════════ BOT
bot = Bot(token=config.BOT_TOKEN) if config.BOT_TOKEN else None
dp = Dispatcher()


def webapp_kb(room: str) -> InlineKeyboardMarkup:
    url = f"{config.WEBAPP_URL}/?room={room}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🏝 ورود به بازی", web_app=WebAppInfo(url=url)),
    ]])


@dp.message(Command("start"))
async def cmd_start(m: Message, command: CommandObject):
    room = "island-main"
    arg = (command.args or "").strip()
    if arg.startswith("room_") and len(arg) > 5:
        room = arg[5:][:64]
    me = await bot.get_me()
    invite = f"https://t.me/{me.username}?start=room_{room}"
    await m.answer(
        f"🎮 <b>Gall Game</b>\n"
        f"به جزیره بقا خوش آمدی، {m.from_user.first_name}!\n\n"
        f"🏝 اتاق: <code>{room}</code>\n\n"
        f"🔗 لینک دعوت:\n{invite}",
        reply_markup=webapp_kb(room), parse_mode="HTML")


@dp.message(Command("room"))
async def cmd_room(m: Message):
    room = "r" + os.urandom(3).hex()
    me = await bot.get_me()
    invite = f"https://t.me/{me.username}?start=room_{room}"
    await m.answer(
        f"🎯 اتاق جدید: <code>{room}</code>\n\n🔗 {invite}",
        reply_markup=webapp_kb(room), parse_mode="HTML")


@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(
        "📖 راهنما:\n"
        "/start — ورود\n/room — ساخت اتاق\n/help — همین پیام\n\n"
        "🎮 در بازی:\n"
        "• روی نقشه به منطقه مجاور برو (هزینه انرژی)\n"
        "• در هر منطقه منابع متفاوتی هست\n"
        "• ابزار بساز تا برداشت بیشتر شود\n"
        "• به بازیکنان همان منطقه حمله کن\n"
        "• در رتبه‌بندی بالا برو!")

# ══════════════════════════════════════════════════════════════ MAIN
async def main():
    config.validate()
    await init_db()
    log.info("DB ready")
    tick_task = asyncio.create_task(game_tick_loop())
    log.info(f"Server on http://{config.HOST}:{config.PORT}")
    log.info(f"WebApp: {config.WEBAPP_URL}")
    server = uvicorn.Server(uvicorn.Config(
        app, host=config.HOST, port=config.PORT, log_level="info", access_log=False))
    try:
        await asyncio.gather(server.serve(), dp.start_polling(bot))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        tick_task.cancel()
        try:
            await bot.session.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
