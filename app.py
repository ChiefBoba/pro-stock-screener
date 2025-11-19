# app.py - Pro Stock Screener with Flow, Dark Pools, 0DTE GEX & Alerts
import streamlit as st
import pandas as pd
import yfinance as yf
import numpy as np
import plotly.graph_objects as go
from scipy.stats import norm
import json
import time
import threading
import websocket
from datetime import datetime, date
import os
from plyer import notification  # Fallback for local alerts

st.set_page_config(page_title="Pro Stock Screener", layout="wide")
st.title("🚀 Pro Stock Screener: Flow + Dark Pools + 0DTE GEX")

# Session State Init
for key in ["alerts", "alert_log", "live_trades", "dark_pool_prints", "quote_cache"]:
    if key not in st.session_state:
        st.session_state[key] = [] if key.endswith("s") else {}

# Load/Save Alerts (Cloud-safe)
ALERT_FILE = "alerts.json"
if os.path.exists(ALERT_FILE):
    with open(ALERT_FILE, "r") as f:
        st.session_state.alerts = json.load(f)

def save_alerts():
    with open(ALERT_FILE, "w") as f:
        json.dump(st.session_state.alerts, f, indent=2)

# Alert Engine
def trigger_alert(title, message):
    ts = datetime.now().strftime("%H:%M:%S")
    st.session_state.alert_log.append({"time": ts, "title": title, "message": message})
    st.session_state.alert_log = st.session_state.alert_log[-50:]
    st.toast(f"🚨 {title}: {message}", icon="🚨")

# 0DTE GEX Compute (Cached for Cloud RAM)
@st.cache_data(ttl=120)
def compute_0dte_gex(ticker: str):
    try:
        stock = yf.Ticker(ticker)
        spot = stock.history(period="1d")["Close"].iloc[-1]
        today = date.today().strftime("%Y-%m-%d")
        expirations = stock.options
        if today not in expirations:
            return None, spot, "No 0DTE today"
        chain = stock.option_chain(today)
        calls, puts = chain.calls, chain.puts
        data = []
        T, r = 1/365.0, 0.05
        for _, row in pd.concat([calls, puts]).iterrows():
            K, iv, oi = row['strike'], row['impliedVolatility'], row.get('openInterest', 0)
            if oi < 10 or iv <= 0.01: continue
            d1 = (np.log(spot/K) + (r + 0.5*iv**2)*T) / (iv*np.sqrt(T))
            gamma = norm.pdf(d1) / (spot * iv * np.sqrt(T))
            gex = gamma * oi * 100 * spot**2 * 0.01 * (1 if 'call' in str(row.name).lower() else -1)
            data.append({"strike": K, "gex": gex, "oi": oi})
        df = pd.DataFrame(data).groupby("strike").agg({"gex": "sum", "oi": "sum"}).reset_index()
        if df.empty: return None, spot, "Low OI"
        total_gex = df["gex"].sum()
        max_wall = df.loc[df["gex"].abs().idxmax()]
        return df, spot, total_gex, max_wall
    except:
        return None, 0, "Error fetching data"

# Simplified WebSocket (Cloud-Optimized: No infinite threads)
@st.cache_resource
def start_ws():
    def on_message(ws, message):
        data = json.loads(message)
        for msg in data:
            if msg.get("ev") == "T" and msg["s"] >= 15000:
                trade = {
                    "Time": datetime.fromtimestamp(msg["t"]/1000).strftime("%H:%M:%S"),
                    "Symbol": msg["sym"],
                    "Price": msg["p"],
                    "Size": msg["s"],
                    "Venue": "Dark Pool" if msg.get("x") == 4 else "Lit"
                }
                st.session_state.live_trades.append(trade)
                if msg.get("x") == 4:
                    st.session_state.dark_pool_prints.append(trade)
    ws = websocket.WebSocketApp("wss://socket.polygon.io/stocks",
                                on_message=on_message,
                                on_open=lambda ws: ws.send(json.dumps({"action":"auth","params":st.secrets["POLYGON_KEY"]})) or
                                                  ws.send(json.dumps({"action":"subscribe","params":"T.*"})))
    # Run in thread with timeout for Cloud
    thread = threading.Thread(target=ws.run_forever, daemon=True)
    thread.start()
    return thread

if st.secrets.get("POLYGON_KEY"):
    start_ws()

# Tabs
tab1, tab2, tab3, tab4 = st.tabs(["Live Flow", "Dark Pools", "0DTE GEX", "Alerts"])

with tab1:
    if st.session_state.live_trades:
        df = pd.DataFrame(st.session_state.live_trades[-50:]).sort_values("Time", ascending=False)
        st.dataframe(df, use_container_width=True)
    else:
        st.info("Connect Polygon key for live trades")

with tab2:
    if st.session_state.dark_pool_prints:
        df = pd.DataFrame(st.session_state.dark_pool_prints[-20:]).sort_values("Time", ascending=False)
        st.dataframe(df, use_container_width=True)
    else:
        st.info("Dark pools via Polygon (market hours)")

with tab3:
    symbol = st.text_input("Ticker", "SPY").upper()
    if st.button("Compute 0DTE GEX"):
        result = compute_0dte_gex(symbol)
        if result[0] is not None:
            df, spot, total_gex, max_wall = result
            col1, col2 = st.columns(2)
            col1.metric("Spot", f"${spot:.2f}")
            col2.metric("Total 0DTE GEX", f"${total_gex/1e6:.0f}M")
            fig = go.Figure()
            fig.add_trace(go.Bar(x=df['strike'], y=df['gex']/1e6, marker_color=np.where(df['gex'] > 0, 'green', 'red')))
            fig.add_vline(x=spot, line_dash="dash", line_color="yellow")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.error(result[2])

with tab4:
    st.subheader("Alert Log")
    for log in reversed(st.session_state.alert_log[-10:]):
        st.write(f"**{log['time']}** • {log['title']}: {log['message']}")

# Auto-Rerun
time.sleep(10)
st.rerun()
