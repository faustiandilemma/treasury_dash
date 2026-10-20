"""
Treasury Management Dashboard (POC)

Tracks capital deployed from treasury wallets into strategy wallets.
Stablecoin-first, multi-asset ready (see PRICES / asset classification).

Run:
    export ETHERSCAN_API_KEY=...        # optional, demo mode works without
    streamlit run treasury_dashboard.py

Data source: Etherscan V2 multichain API (one key, chainid switch).
Swap `EtherscanClient` for Covalent/Alchemy/an indexer without touching the UI.
"""

from __future__ import annotations

import os
import time
import json
import random
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests
import streamlit as st
import altair as alt

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

ETHERSCAN_BASE = "https://api.etherscan.io/v2/api"

CHAINS = {
    "Ethereum": 1,
    "Base": 8453,
    "Arbitrum One": 42161,
    "Optimism": 10,
    "Polygon": 137,
    "BNB Chain": 56,
    "Avalanche": 43114,
}

EXPLORERS = {
    1: "https://etherscan.io",
    8453: "https://basescan.org",
    42161: "https://arbiscan.io",
    10: "https://optimistic.etherscan.io",
    137: "https://polygonscan.com",
    56: "https://bscscan.com",
    43114: "https://snowscan.xyz",
}

STABLES = {
    "USDC", "USDC.E", "USDT", "USDT0", "USDT.E", "DAI", "USDE", "SUSDE",
    "PYUSD", "FDUSD", "USDS", "SUSDS", "CRVUSD", "GHO", "LUSD", "TUSD",
    "USDP", "RLUSD", "USD0", "USD0++", "FRAX", "SFRAX", "BUIDL", "USDY",
    "USYC", "DEUSD", "USDF", "USDL", "MIM", "BUSD", "AUSDC", "AUSDT",
}

# Multi-asset hook. Stables default to 1.0; add a price feed here later
# (Coingecko / internal mark service) and USD aggregates light up for everything.
PRICE_OVERRIDES: dict[str, float] = {}

DEFAULT_TREASURY = {
    "Treasury 1": "0xB024aC1bC3DBCD3cEc869F77E13F9D7EC00c3A87",
}
DEFAULT_STRATEGY = {
    "Strategy 1": "0x2377a430Cc45e6C0Bfe4Aa0783D47FB02f32FC5e",
    "Strategy 2": "0x5e13D8BBA207a9c21BCE74263d8A2079a143440A",
}

st.set_page_config(page_title="Treasury Dashboard", page_icon="🏦", layout="wide")


# ----------------------------------------------------------------------------
# Data layer
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Wallet:
    label: str
    address: str
    kind: str  # "treasury" | "strategy"

    @property
    def short(self) -> str:
        return f"{self.address[:6]}...{self.address[-4:]}"


class RateLimiter:
    """Free tier is 5 req/s. Cheap client-side throttle."""

    def __init__(self, per_second: float = 4.0):
        self.min_gap = 1.0 / per_second
        self.last = 0.0

    def wait(self):
        gap = time.monotonic() - self.last
        if gap < self.min_gap:
            time.sleep(self.min_gap - gap)
        self.last = time.monotonic()


_limiter = RateLimiter()


@st.cache_data(ttl=300, show_spinner=False)
def fetch_token_transfers(address: str, chain_id: int, api_key: str) -> list[dict]:
    """All ERC-20 transfers touching `address`. Block-cursor paginated."""
    out: list[dict] = []
    seen: set[tuple] = set()
    start_block = 0
    page_size = 10_000

    for _ in range(50):  # hard stop, 500k transfers
        _limiter.wait()
        params = {
            "chainid": chain_id,
            "module": "account",
            "action": "tokentx",
            "address": address,
            "startblock": start_block,
            "endblock": 99999999,
            "page": 1,
            "offset": page_size,
            "sort": "asc",
            "apikey": api_key,
        }
        r = requests.get(ETHERSCAN_BASE, params=params, timeout=30)
        r.raise_for_status()
        payload = r.json()

        if payload.get("status") != "1":
            msg = str(payload.get("result") or payload.get("message") or "")
            if "No transactions found" in msg or payload.get("message") == "No transactions found":
                break
            raise RuntimeError(f"Etherscan error for {address}: {msg}")

        rows = payload.get("result") or []
        new = 0
        for row in rows:
            key = (row["hash"], row["from"], row["to"], row["value"], row["contractAddress"])
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
            new += 1

        if len(rows) < page_size or new == 0:
            break
        start_block = int(rows[-1]["blockNumber"])

    return out


def synthetic_transfers(wallets: list[Wallet], chain_id: int) -> list[dict]:
    """Deterministic fake history so the POC is demoable without an API key."""
    rng = random.Random(7)
    treasury = [w for w in wallets if w.kind == "treasury"]
    strategies = [w for w in wallets if w.kind == "strategy"]
    if not treasury or not strategies:
        return []

    tokens = [
        ("USDC", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6),
        ("USDT", "0xdac17f958d2ee523a2206206994597c13d831ec7", 6),
    ]
    venues = [
        "0x1111111111111111111111111111111111111111",
        "0x2222222222222222222222222222222222222222",
        "0x3333333333333333333333333333333333333333",
    ]
    funder = "0x9999999999999999999999999999999999999999"

    rows: list[dict] = []
    now = datetime.now(timezone.utc)
    t0 = now - timedelta(days=180)
    block = 20_000_000

    def emit(ts, frm, to, sym, addr, dec, amt):
        nonlocal block
        if ts > now:
            return
        block += rng.randint(50, 400)
        rows.append({
            "blockNumber": str(block),
            "timeStamp": str(int(ts.timestamp())),
            "hash": "0x" + f"{rng.getrandbits(256):064x}",
            "from": frm,
            "to": to,
            "contractAddress": addr,
            "value": str(int(amt * 10 ** dec)),
            "tokenName": sym,
            "tokenSymbol": sym,
            "tokenDecimal": str(dec),
        })

    # seed the treasury so idle capital and utilization are meaningful
    for sym, addr, dec in tokens:
        emit(t0 - timedelta(days=5), funder, treasury[0].address, sym, addr, dec, 10_000_000)

    for i, strat in enumerate(strategies):
        ts = t0 + timedelta(days=i * 4)
        for k in range(4):
            sym, addr, dec = tokens[k % len(tokens)]
            amt = rng.choice([250_000, 500_000, 1_000_000, 1_500_000])
            emit(ts, treasury[0].address, strat.address, sym, addr, dec, amt)
            ts += timedelta(days=rng.randint(4, 10))
            v = rng.choice(venues)
            emit(ts, strat.address, v, sym, addr, dec, amt * 0.9)
            ts += timedelta(days=rng.randint(5, 15))
            emit(ts, v, strat.address, sym, addr, dec, amt * 0.9 * (1 + rng.uniform(0.01, 0.06)))
            ts += timedelta(days=rng.randint(3, 10))
        sym, addr, dec = tokens[0]
        emit(ts, strat.address, treasury[0].address, sym, addr, dec, 300_000)
    return rows


# ----------------------------------------------------------------------------
# Ledger construction
# ----------------------------------------------------------------------------

def classify_counterparty(addr: str, index: dict[str, Wallet]) -> tuple[str, str]:
    w = index.get(addr.lower())
    if w:
        return w.kind, w.label
    return "external", ""


def build_ledger(raw_by_wallet: dict[str, list[dict]], wallets: list[Wallet]) -> pd.DataFrame:
    index = {w.address.lower(): w for w in wallets}
    records = []

    for addr, rows in raw_by_wallet.items():
        w = index[addr.lower()]
        for r in rows:
            frm, to = r["from"].lower(), r["to"].lower()
            if frm == addr.lower():
                direction, cp = "OUT", to
            elif to == addr.lower():
                direction, cp = "IN", frm
            else:
                continue
            dec = int(r.get("tokenDecimal") or 18)
            amt = int(r["value"]) / (10 ** dec)
            cp_kind, cp_label = classify_counterparty(cp, index)
            sym = (r.get("tokenSymbol") or "?").upper()
            records.append({
                "timestamp": datetime.fromtimestamp(int(r["timeStamp"]), tz=timezone.utc),
                "block": int(r["blockNumber"]),
                "tx_hash": r["hash"],
                "wallet": w.label,
                "wallet_kind": w.kind,
                "wallet_address": w.address,
                "direction": direction,
                "signed_amount": amt if direction == "IN" else -amt,
                "amount": amt,
                "asset": sym,
                "contract": r["contractAddress"],
                "counterparty": cp,
                "counterparty_kind": cp_kind,
                "counterparty_label": cp_label or cp[:10] + "...",
            })

    df = pd.DataFrame.from_records(records)
    if df.empty:
        return df

    df["is_stable"] = df["asset"].isin(STABLES)
    df["price"] = df["asset"].map(lambda s: PRICE_OVERRIDES.get(s, 1.0 if s in STABLES else float("nan")))
    df["usd"] = df["amount"] * df["price"]
    df["signed_usd"] = df["signed_amount"] * df["price"]
    df["date"] = df["timestamp"].dt.date
    return df.sort_values("timestamp").reset_index(drop=True)


def treasury_flows(df: pd.DataFrame) -> pd.DataFrame:
    """Strategy-side view of transfers whose counterparty is a treasury wallet."""
    if df.empty:
        return df
    return df[(df["wallet_kind"] == "strategy") & (df["counterparty_kind"] == "treasury")]


def strategy_summary(df: pd.DataFrame, wallets: list[Wallet]) -> pd.DataFrame:
    strategies = [w for w in wallets if w.kind == "strategy"]
    tf = treasury_flows(df)
    rows = []
    now = pd.Timestamp.now(tz="UTC")

    for s in strategies:
        sub = df[df["wallet"] == s.label]
        flows = tf[tf["wallet"] == s.label]
        deployed = flows.loc[flows.direction == "IN", "usd"].sum()
        returned = flows.loc[flows.direction == "OUT", "usd"].sum()
        net = deployed - returned
        balance = sub["signed_usd"].sum()  # reconstructed from full transfer history
        external_out = sub[(sub.counterparty_kind == "external") & (sub.direction == "OUT")]["usd"].sum()
        external_in = sub[(sub.counterparty_kind == "external") & (sub.direction == "IN")]["usd"].sum()
        pnl = balance - net
        first = flows["timestamp"].min() if not flows.empty else pd.NaT
        last = sub["timestamp"].max() if not sub.empty else pd.NaT
        days = (now - first).days if pd.notna(first) else 0

        rows.append({
            "Strategy": s.label,
            "Address": s.address,
            "Net deployed": net,
            "Gross deployed": deployed,
            "Returned to treasury": returned,
            "Current balance": balance,
            "Implied P&L": pnl,
            "Return %": (pnl / net * 100) if net else float("nan"),
            "Ann. (Dietz) %": modified_dietz(flows, balance) * 100 if net else float("nan"),
            "Deployed to venues": external_out,
            "Received from venues": external_in,
            "Transfers": len(sub),
            "First funded": first,
            "Last activity": last,
            "Days since activity": (now - last).days if pd.notna(last) else None,
            "Days deployed": days,
        })
    return pd.DataFrame(rows)


def modified_dietz(flows: pd.DataFrame, end_value: float) -> float:
    """Annualized time-weighted-ish return using treasury flows as the only cashflows."""
    if flows.empty:
        return float("nan")
    start = flows["timestamp"].min()
    end = pd.Timestamp.now(tz="UTC")
    total_days = max((end - start).days, 1)

    cf = flows.assign(signed=lambda d: d["usd"].where(d.direction == "IN", -d["usd"]))
    net_cf = cf["signed"].sum()
    weights = cf["timestamp"].map(lambda t: (end - t).days / total_days)
    weighted = float((cf["signed"] * weights).sum())
    denom = weighted
    if denom <= 0:
        return float("nan")
    r = (end_value - net_cf) / denom
    return (1 + r) ** (365 / total_days) - 1 if r > -1 else float("nan")


def running_balance(df: pd.DataFrame, label: str) -> pd.DataFrame:
    sub = df[df["wallet"] == label].sort_values("timestamp").copy()
    if sub.empty:
        return sub
    sub["balance"] = sub["signed_usd"].cumsum()
    tf = sub[sub["counterparty_kind"] == "treasury"].copy()
    sub["net_deployed"] = 0.0
    sub.loc[tf.index, "net_deployed"] = tf["signed_usd"]
    sub["net_deployed"] = sub["net_deployed"].cumsum()
    return sub


def fmt_usd(x) -> str:
    if pd.isna(x):
        return "-"
    return f"${x:,.0f}"


# ----------------------------------------------------------------------------
# Sidebar
# ----------------------------------------------------------------------------

def parse_wallets(text: str, kind: str) -> list[Wallet]:
    out = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "," in line:
            label, addr = [p.strip() for p in line.split(",", 1)]
        else:
            label, addr = line[:10] + "...", line
        if addr.startswith("0x") and len(addr) == 42:
            out.append(Wallet(label=label, address=addr, kind=kind))
    return out


with st.sidebar:
    st.header("Configuration")

    chain_name = st.selectbox("Chain", list(CHAINS), index=0)
    chain_id = CHAINS[chain_name]
    explorer = EXPLORERS[chain_id]

    api_key = st.text_input(
        "Etherscan API key",
        value=os.environ.get("ETHERSCAN_API_KEY", ""),
        type="password",
        help="One V2 key covers all chains. Leave blank to run on demo data.",
    )
    demo = st.toggle("Demo mode (synthetic data)", value=not bool(api_key))

    st.divider()
    st.caption("One wallet per line, `Label, 0xaddress`")
    treasury_text = st.text_area(
        "Treasury wallets",
        "\n".join(f"{k}, {v}" for k, v in DEFAULT_TREASURY.items()),
        height=80,
    )
    strategy_text = st.text_area(
        "Strategy wallets",
        "\n".join(f"{k}, {v}" for k, v in DEFAULT_STRATEGY.items()),
        height=110,
    )

    st.divider()
    asset_mode = st.radio("Assets", ["Stablecoins only", "All assets"], index=0)
    large_threshold = st.number_input("Large transfer alert ($)", value=1_000_000, step=100_000)
    idle_days = st.number_input("Idle wallet alert (days)", value=30, step=5)

    if st.button("Refresh data", width="stretch"):
        st.cache_data.clear()
        st.rerun()

wallets = parse_wallets(treasury_text, "treasury") + parse_wallets(strategy_text, "strategy")

if not wallets:
    st.warning("Add at least one treasury and one strategy wallet.")
    st.stop()

# ----------------------------------------------------------------------------
# Load
# ----------------------------------------------------------------------------

raw: dict[str, list[dict]] = {}
errors: list[str] = []

if demo:
    rows = synthetic_transfers(wallets, chain_id)
    for w in wallets:
        a = w.address.lower()
        raw[w.address] = [r for r in rows if r["from"].lower() == a or r["to"].lower() == a]
else:
    if not api_key:
        st.error("Provide an Etherscan API key or enable demo mode.")
        st.stop()
    prog = st.progress(0.0, text="Pulling transfers...")
    for i, w in enumerate(wallets):
        try:
            raw[w.address] = fetch_token_transfers(w.address, chain_id, api_key)
        except Exception as e:  # noqa: BLE001
            raw[w.address] = []
            errors.append(f"{w.label}: {e}")
        prog.progress((i + 1) / len(wallets), text=f"Pulling transfers... {w.label}")
    prog.empty()

for e in errors:
    st.error(e)

ledger = build_ledger(raw, wallets)

if ledger.empty:
    st.info("No ERC-20 transfers found for these wallets on this chain.")
    st.stop()

if asset_mode == "Stablecoins only":
    ledger = ledger[ledger["is_stable"]].copy()

unpriced = sorted(ledger.loc[ledger["price"].isna(), "asset"].unique())
if unpriced:
    st.warning(f"No price for {', '.join(unpriced)} — excluded from USD totals. Add to PRICE_OVERRIDES.")
    ledger = ledger[ledger["price"].notna()].copy()

summary = strategy_summary(ledger, wallets)
tf = treasury_flows(ledger)

# ----------------------------------------------------------------------------
# Header
# ----------------------------------------------------------------------------

st.title("Treasury Management Dashboard")
st.caption(
    f"{chain_name} · {len(wallets)} wallets · {len(ledger):,} transfers · "
    f"{'demo data' if demo else 'live'} · {asset_mode.lower()}"
)

c1, c2, c3, c4, c5 = st.columns(5)
treas_balance = ledger[ledger.wallet_kind == "treasury"]["signed_usd"].sum()
c1.metric("Treasury balance", fmt_usd(treas_balance))
c2.metric("Net deployed", fmt_usd(summary["Net deployed"].sum()))
c3.metric("Strategy balances", fmt_usd(summary["Current balance"].sum()))
c4.metric("Implied P&L", fmt_usd(summary["Implied P&L"].sum()))
deployed_total = summary["Net deployed"].sum()
denom = max(treas_balance, 0) + deployed_total
util = (deployed_total / denom * 100) if denom else float("nan")
c5.metric("Capital utilization", f"{util:.1f}%" if pd.notna(util) else "-",
          help="Net deployed / (idle treasury + net deployed)")

tab_overview, tab_detail, tab_flows, tab_alerts, tab_ledger = st.tabs(
    ["Overview", "Strategy detail", "Flow matrix", "Alerts", "Raw ledger"]
)

# ----------------------------------------------------------------------------
# Overview
# ----------------------------------------------------------------------------

with tab_overview:
    st.subheader("Strategy positions")
    st.caption("Click a row to open it in Strategy detail.")

    display = summary[[
        "Strategy", "Net deployed", "Current balance", "Implied P&L", "Return %",
        "Ann. (Dietz) %", "Gross deployed", "Returned to treasury",
        "Transfers", "Days since activity",
    ]]

    event = st.dataframe(
        display,
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "Net deployed": st.column_config.NumberColumn(format="$%.0f"),
            "Current balance": st.column_config.NumberColumn(format="$%.0f"),
            "Implied P&L": st.column_config.NumberColumn(format="$%.0f"),
            "Gross deployed": st.column_config.NumberColumn(format="$%.0f"),
            "Returned to treasury": st.column_config.NumberColumn(format="$%.0f"),
            "Return %": st.column_config.NumberColumn(format="%.2f%%"),
            "Ann. (Dietz) %": st.column_config.NumberColumn(format="%.2f%%"),
        },
    )
    if event.selection.rows:
        st.session_state["selected_strategy"] = summary.iloc[event.selection.rows[0]]["Strategy"]
        st.info(f"Selected **{st.session_state['selected_strategy']}** — open the Strategy detail tab.")

    left, right = st.columns(2)

    with left:
        st.subheader("Cumulative net deployed")
        series = []
        for s in summary["Strategy"]:
            rb = running_balance(ledger, s)
            if rb.empty:
                continue
            series.append(rb[["timestamp", "net_deployed"]].assign(Strategy=s))
        if series:
            chart_df = pd.concat(series)
            st.altair_chart(
                alt.Chart(chart_df).mark_line(interpolate="step-after").encode(
                    x=alt.X("timestamp:T", title=None),
                    y=alt.Y("net_deployed:Q", title="USD", axis=alt.Axis(format="$,.0s")),
                    color=alt.Color("Strategy:N", legend=alt.Legend(orient="bottom")),
                    tooltip=["Strategy", "timestamp:T", alt.Tooltip("net_deployed:Q", format="$,.0f")],
                ).properties(height=300),
                width="stretch",
            )

    with right:
        st.subheader("Balance vs deployed capital")
        series = []
        for s in summary["Strategy"]:
            rb = running_balance(ledger, s)
            if rb.empty:
                continue
            series.append(rb[["timestamp", "balance"]].assign(Strategy=s))
        if series:
            chart_df = pd.concat(series)
            st.altair_chart(
                alt.Chart(chart_df).mark_line().encode(
                    x=alt.X("timestamp:T", title=None),
                    y=alt.Y("balance:Q", title="USD", axis=alt.Axis(format="$,.0s")),
                    color=alt.Color("Strategy:N", legend=alt.Legend(orient="bottom")),
                    tooltip=["Strategy", "timestamp:T", alt.Tooltip("balance:Q", format="$,.0f")],
                ).properties(height=300),
                width="stretch",
            )

    st.subheader("Net deployed by asset")
    by_asset = (
        tf.assign(signed=lambda d: d["usd"].where(d.direction == "IN", -d["usd"]))
        .pivot_table(index="wallet", columns="asset", values="signed", aggfunc="sum", fill_value=0)
    )
    st.dataframe(by_asset.style.format("${:,.0f}"), width="stretch")

# ----------------------------------------------------------------------------
# Strategy detail
# ----------------------------------------------------------------------------

with tab_detail:
    options = list(summary["Strategy"])
    default_idx = options.index(st.session_state["selected_strategy"]) \
        if st.session_state.get("selected_strategy") in options else 0
    chosen = st.selectbox("Strategy", options, index=default_idx)
    row = summary[summary["Strategy"] == chosen].iloc[0]

    st.markdown(f"`{row['Address']}` · [explorer]({explorer}/address/{row['Address']})")

    m = st.columns(6)
    m[0].metric("Net deployed", fmt_usd(row["Net deployed"]))
    m[1].metric("Balance", fmt_usd(row["Current balance"]))
    m[2].metric("Implied P&L", fmt_usd(row["Implied P&L"]),
                f"{row['Return %']:.2f}%" if pd.notna(row["Return %"]) else None)
    m[3].metric("Days deployed", int(row["Days deployed"]))
    m[4].metric("Deployed to venues", fmt_usd(row["Deployed to venues"]))
    m[5].metric("Last activity",
                row["Last activity"].strftime("%Y-%m-%d") if pd.notna(row["Last activity"]) else "-")

    st.subheader("Treasury transfers")
    detail = tf[tf["wallet"] == chosen].copy()
    detail["Direction"] = detail["direction"].map({"IN": "Funding", "OUT": "Return"})
    detail["Explorer"] = detail["tx_hash"].map(lambda h: f"{explorer}/tx/{h}")
    view = detail[["timestamp", "Direction", "asset", "amount", "usd",
                   "counterparty_label", "tx_hash", "Explorer"]].rename(columns={
        "timestamp": "Time (UTC)", "asset": "Asset", "amount": "Amount",
        "usd": "USD", "counterparty_label": "Treasury wallet", "tx_hash": "Tx",
    })
    st.dataframe(
        view.sort_values("Time (UTC)", ascending=False),
        hide_index=True,
        width="stretch",
        column_config={
            "Amount": st.column_config.NumberColumn(format="%.2f"),
            "USD": st.column_config.NumberColumn(format="$%.0f"),
            "Explorer": st.column_config.LinkColumn(display_text="view"),
        },
    )

    st.subheader("All wallet activity (incl. venues)")
    all_act = ledger[ledger["wallet"] == chosen].copy()
    all_act["Explorer"] = all_act["tx_hash"].map(lambda h: f"{explorer}/tx/{h}")
    st.dataframe(
        all_act[["timestamp", "direction", "asset", "amount", "usd",
                 "counterparty_kind", "counterparty_label", "Explorer"]]
        .sort_values("timestamp", ascending=False)
        .rename(columns={"timestamp": "Time (UTC)", "direction": "Dir", "asset": "Asset",
                         "amount": "Amount", "usd": "USD",
                         "counterparty_kind": "CP type", "counterparty_label": "Counterparty"}),
        hide_index=True,
        width="stretch",
        column_config={
            "Amount": st.column_config.NumberColumn(format="%.2f"),
            "USD": st.column_config.NumberColumn(format="$%.0f"),
            "Explorer": st.column_config.LinkColumn(display_text="view"),
        },
    )

    st.subheader("Counterparty concentration")
    cp = (all_act[all_act.counterparty_kind == "external"]
          .groupby("counterparty")
          .agg(volume=("usd", "sum"), transfers=("usd", "size"),
               last_seen=("timestamp", "max"))
          .sort_values("volume", ascending=False)
          .reset_index())
    if cp.empty:
        st.caption("No external counterparties.")
    else:
        cp["Explorer"] = cp["counterparty"].map(lambda a: f"{explorer}/address/{a}")
        st.dataframe(cp, hide_index=True, width="stretch", column_config={
            "volume": st.column_config.NumberColumn("Volume", format="$%.0f"),
            "Explorer": st.column_config.LinkColumn(display_text="view"),
        })

    st.download_button(
        "Download strategy ledger (CSV)",
        all_act.to_csv(index=False).encode(),
        file_name=f"{chosen.replace(' ', '_')}_ledger.csv",
        mime="text/csv",
    )

# ----------------------------------------------------------------------------
# Flow matrix
# ----------------------------------------------------------------------------

with tab_flows:
    st.subheader("Treasury to strategy net flows")
    matrix = (
        tf.assign(signed=lambda d: d["usd"].where(d.direction == "IN", -d["usd"]))
        .pivot_table(index="counterparty_label", columns="wallet", values="signed",
                     aggfunc="sum", fill_value=0)
    )
    matrix["Total"] = matrix.sum(axis=1)
    matrix.loc["Total"] = matrix.sum()
    st.dataframe(matrix.style.format("${:,.0f}"), width="stretch")

    st.subheader("Monthly funding cadence")
    monthly = tf.copy()
    monthly["month"] = monthly["timestamp"].dt.tz_localize(None).dt.to_period("M").dt.to_timestamp()
    monthly["signed"] = monthly["usd"].where(monthly.direction == "IN", -monthly["usd"])
    agg = monthly.groupby(["month", "wallet"], as_index=False)["signed"].sum()
    if not agg.empty:
        st.altair_chart(
            alt.Chart(agg).mark_bar().encode(
                x=alt.X("yearmonth(month):O", title=None),
                y=alt.Y("signed:Q", title="Net flow (USD)", axis=alt.Axis(format="$,.0s")),
                color=alt.Color("wallet:N", title="Strategy", legend=alt.Legend(orient="bottom")),
                tooltip=["wallet", alt.Tooltip("signed:Q", format="$,.0f")],
            ).properties(height=300),
            width="stretch",
        )

    st.subheader("Reconciliation")
    recon = summary[["Strategy", "Net deployed", "Deployed to venues",
                     "Received from venues", "Current balance", "Implied P&L"]].copy()
    recon["Check"] = (
        recon["Net deployed"] - recon["Deployed to venues"]
        + recon["Received from venues"] - recon["Current balance"]
    ).round(2)
    st.dataframe(recon, hide_index=True, width="stretch", column_config={
        c: st.column_config.NumberColumn(format="$%.0f")
        for c in recon.columns if c != "Strategy"
    })
    st.caption(
        "Balance is reconstructed from the full transfer history. "
        "Check should be ~0; non-zero means missing history, rebasing tokens, or an unpriced asset."
    )

# ----------------------------------------------------------------------------
# Alerts
# ----------------------------------------------------------------------------

with tab_alerts:
    st.subheader("Exceptions")
    alerts = []

    big = ledger[ledger["usd"] >= large_threshold]
    for _, r in big.sort_values("timestamp", ascending=False).head(25).iterrows():
        alerts.append({
            "Severity": "Info", "Type": "Large transfer",
            "Detail": f"{r['wallet']} {r['direction']} {fmt_usd(r['usd'])} {r['asset']} "
                      f"with {r['counterparty_label']}",
            "When": r["timestamp"],
        })

    for _, r in summary.iterrows():
        if r["Days since activity"] is not None and r["Days since activity"] > idle_days and r["Current balance"] > 0:
            alerts.append({
                "Severity": "Warn", "Type": "Idle capital",
                "Detail": f"{r['Strategy']} holds {fmt_usd(r['Current balance'])} "
                          f"with no activity for {int(r['Days since activity'])} days",
                "When": r["Last activity"],
            })
        if pd.notna(r["Implied P&L"]) and r["Net deployed"] and r["Implied P&L"] < -0.02 * r["Net deployed"]:
            alerts.append({
                "Severity": "Warn", "Type": "Drawdown",
                "Detail": f"{r['Strategy']} below funded capital by {fmt_usd(abs(r['Implied P&L']))} "
                          f"({r['Return %']:.2f}%)",
                "When": r["Last activity"],
            })

    # Strategy sending funds to an address that is neither treasury nor a known venue
    first_seen = (ledger[ledger.counterparty_kind == "external"]
                  .groupby("counterparty")["timestamp"].min())
    recent_new = first_seen[first_seen > pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=14)]
    for addr, ts in recent_new.items():
        alerts.append({
            "Severity": "Info", "Type": "New counterparty",
            "Detail": f"First interaction with {addr}",
            "When": ts,
        })

    if alerts:
        adf = pd.DataFrame(alerts).sort_values("When", ascending=False)
        st.dataframe(adf, hide_index=True, width="stretch")
    else:
        st.success("No exceptions.")

    st.subheader("Asset exposure by wallet")
    exposure = ledger.pivot_table(index="wallet", columns="asset",
                                  values="signed_usd", aggfunc="sum", fill_value=0)
    st.dataframe(exposure.style.format("${:,.0f}"), width="stretch")

# ----------------------------------------------------------------------------
# Raw ledger
# ----------------------------------------------------------------------------

with tab_ledger:
    f1, f2, f3 = st.columns(3)
    w_filter = f1.multiselect("Wallet", sorted(ledger["wallet"].unique()))
    a_filter = f2.multiselect("Asset", sorted(ledger["asset"].unique()))
    k_filter = f3.multiselect("Counterparty type", ["treasury", "strategy", "external"])

    view = ledger.copy()
    if w_filter:
        view = view[view["wallet"].isin(w_filter)]
    if a_filter:
        view = view[view["asset"].isin(a_filter)]
    if k_filter:
        view = view[view["counterparty_kind"].isin(k_filter)]

    view["Explorer"] = view["tx_hash"].map(lambda h: f"{explorer}/tx/{h}")
    st.dataframe(
        view[["timestamp", "wallet", "direction", "asset", "amount", "usd",
              "counterparty_kind", "counterparty_label", "block", "Explorer"]]
        .sort_values("timestamp", ascending=False),
        hide_index=True, width="stretch",
        column_config={
            "amount": st.column_config.NumberColumn(format="%.4f"),
            "usd": st.column_config.NumberColumn(format="$%.0f"),
            "Explorer": st.column_config.LinkColumn(display_text="view"),
        },
    )
    st.download_button("Download full ledger (CSV)", view.to_csv(index=False).encode(),
                       file_name="treasury_ledger.csv", mime="text/csv")
