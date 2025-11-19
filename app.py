# app.py - Pro Stock Screener: Flow + Dark Pools + 0DTE GEX + Unusual Options (NO NEWS)
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

st.set_page_config(page_title="Pro Stock Screener", layout="wide")
st.title("Pro Stock Screener: Flow + Dark Pools + 0DTE GEX")

# === SESSION STATE ===
for key in ["alerts", "alert_log", "live_trades", "dark_pool_prints"]:
    if key not in st.session_state:
        st.session_state[key] = [] if key.endswith("s") else {}

# === ALERT ENGINE ===
def trigger_alert(title, message):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = {"time": ts, "title": title, "message": message}
    st.session_state.alert_log.append(entry)
    st.session_state.alert_log = st.session_state.alert_log[-50:]
    st.toast(f"{title}: {message}", icon="")

# === 0DTE GEX + UNUSUAL OPTIONS (Volume > OI) ===
@st.cache_data(ttl=120, show_spinner="Computing 0DTE GEX...")
def compute_0dte_gex_and_unusual(ticker: str):
    try:
        stock = yf.Ticker(ticker)
        spot = stock.history(period="1d")["Close"].iloc[-1]
        today_str = date.today().strftime("%Y-%m-%d")
        if today_str not in stock.options:
            return None, spot, "No 0DTE today", None

        chain = stock.option_chain(today_str)
        calls = chain.calls.assign(type="Call")
        puts = chain.puts.assign(type="Put")
        df = pd.concat([calls, puts])

        data = []
        unusual = []
        T, r = 1/365.0, 0.05

        for _, row in df.iterrows():
            K = row["strike"]
            iv = row["impliedVolatility"]
            oi = row.get("openInterest", 0) or 0
            vol = row.get("volume", 0) or 0

            if oi < 10 or iv <= 0.01:
                continue

            d1 = (np.log(spot/K) + (r + 0.5*iv**2)*T) / (iv*np.sqrt(T))
            gamma = norm.pdf(d1) / (spot * iv * np.sqrt(T))
            gex = gamma * oi * 100 * spot**2 * 0.01
            if row["type"] == "Put":
                gex = -gex

            data.append({"strike": K, "type": row["type"], "oi": oi, "volume": vol, "gex": gex})

            if vol > oi and oi > 0:
                unusual.append({
                    "strike": K,
                    "type": row["type"],
                    "volume": int(vol),
                    "oi": int(oi),
                    "ratio": round(vol/oi, 2)
                })

        gex_df = pd.DataFrame(data)
        agg = gex_df.groupby("strike").agg({"gex": "sum", "oi": "sum"}).reset_index()
        total_gex = agg["gex"].sum()
        max_wall = agg.loc[agg["gex"].abs().idxmax()]

        unusual_df = pd.DataFrame(unusual) if unusual else None

        return agg, spot, total_gex, max_wall, unusual_df

    except Exception as e:
        return None, 0, f"Error: {str(e)}", None, None

# === POLYGON WEBSOCKET ===
@st.cache_resource
def start_polygon_websocket():
    def on_message(ws, message):
        data = json.loads(message)
        for msg in data:
            if msg.get("ev") == "T" and msg.get("s", 0) >= 15000:
                trade = {
                    "Time": datetime.fromtimestamp(msg["t"]/1000).strftime("%H:%M:%S"),
                    "Symbol": msg["sym"],
                    "Price": f"${msg['p']:.2f}",
                    "Size": f"{msg['s']:,}",
                    "Venue": "Dark Pool" if msg.get("x") == 4 else "Lit"
                }
                st.session_state.live_trades.append(trade)
                st.session_state.live_trades = st.session_state.live_trades[-200:]
                if msg.get("x") == 4:
                    st.session_state.dark_pool_prints.append(trade)
                    st.session_state.dark_pool_prints = st.session_state.dark_pool_prints[-100:]

    key = st.secrets.get("POLYGON_KEY")
    if not key:
        return

    ws = websocket.WebSocketApp(
        "wss://socket.polygon.io/stocks",
        on_message=on_message,
        on_open=lambda ws: (
            ws.send(json.dumps({"action": "auth", "params": key})),
            ws.send(json.dumps({"action": "subscribe", "params": "T.*"}))
        )
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()

if st.secrets.get("POLYGON_KEY"):
    start_polygon_websocket()

# === TABS ===
tab1, tab2, tab3, tab4 = st.tabs(["Live Flow", "Dark Pools", "0DTE GEX + Unusual", "Alerts"])

with tab1:
    st.subheader("Real-Time Large Blocks (>15k shares)")
    if st.session_state.live_trades:
        df = pd.DataFrame(st.session_state.live_trades).sort_values("Time", ascending=False)
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("Add POLYGON_KEY in Secrets → live trades appear instantly")

with tab2:
    st.subheader("Real-Time Dark Pool Prints")
    if st.session_state.dark_pool_prints:
        df = pd.DataFrame(st.session_state.dark_pool_prints).sort_values("Time", ascending=False)
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("Dark prints appear during market hours")

with tab3:
    st.subheader("0DTE Gamma Exposure + Unusual Options Activity")
    ticker = st.text_input("Ticker", "SPY", key="gex").upper()
    if st.button("Compute 0DTE GEX", type="primary"):
        st.rerun()

    result = compute_0dte_gex_and_unusual(ticker)
    if result[0] is None:
        st.warning(result[2])
    else:
        agg, spot, total_gex, max_wall, unusual_df = result

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Spot", f"${spot:.2f}")
        c2.metric("Total GEX", f"${total_gex/1e6:.1f}M")
        c3.metric("Biggest Wall", f"${max_wall['strike']}")
        c4.metric("Wall GEX", f"${max_wall['gex']/1e6:.1f}M")

        fig = go.Figure()
        pos = agg[agg["gex"] > 0]
        neg = agg[agg["gex"] < 0]
        fig.add_trace(go.Bar(x=pos["strike"], y=pos["gex"]/1e6, name="Positive GEX", marker_color="limegreen"))
        fig.add_trace(go.Bar(x=neg["strike"], y=neg["gex"]/1e6, name="Negative GEX", marker_color="crimson"))
        fig.add_vline(x=spot, line=dict(color="yellow", width=3, dash="dash"))
        fig.update_layout(title=f"{ticker} 0DTE GEX", xaxis_title="Strike", yaxis_title="GEX ($M per 1%)", barmode="relative", template="plotly_dark", height=600)
        st.plotly_chart(fig, use_container_width=True)

        if unusual_df is not None and not unusual_df.empty:
            st.success(f"Unusual Options – {len(unusual_df)} strikes with Volume > OI!")
            unusual_df = unusual_df.sort_values("ratio", ascending=False)
            st.dataframe(unusual_df[["strike","type","volume","oi","ratio"]], use_container_width=True, hide_index=True)
        else:
            st.info("No unusual options activity right now")

with tab4:
    st.subheader("Live Alert Log")
    if st.session_state.alert_log:
        for log in reversed(st.session_state.alert_log[-20:]):
            st.write(f"**{log['time']}** • **{log['title']}**: {log['message']}")
    else:
        st.info("Alerts will appear here in real time")

# Auto-refresh
time.sleep(20)
st.rerun()
