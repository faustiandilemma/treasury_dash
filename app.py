"""
USD Stablecoin Treasury Dashboard (POC)

Workbook metrics, derived live from wallet activity.

Everything on screen is computed from ERC-20 transfer history pulled from the
Etherscan V2 API. Nothing is keyed in. Opening balances, current composition,
net deployed and strategy wallet value are all reconstructed from the log.

Spoofed tokens are quarantined rather than dropped. See TOKEN REGISTRY below:
only transfers in contracts on the allowlist reach the headline numbers.

Run:
    streamlit run app2.py
"""

from __future__ import annotations

import os
import time
import random
import unicodedata
from datetime import datetime, timezone, timedelta, date

import altair as alt
import pandas as pd
import requests
import streamlit as st

ETHERSCAN_BASE = "https://api.etherscan.io/v2/api"
HARDCODED_API_KEY = "PU98SQABT31XGYWM7W6D66JWS4D3WPSUQ6"

CHAINS = {"Ethereum": 1, "Arbitrum One": 42161, "Polygon": 137, "Base": 8453,
          "Optimism": 10, "BNB Chain": 56, "Avalanche": 43114}
EXPLORERS = {1: "https://etherscan.io", 42161: "https://arbiscan.io",
             137: "https://polygonscan.com", 8453: "https://basescan.org",
             10: "https://optimistic.etherscan.io", 56: "https://bscscan.com",
             43114: "https://snowscan.xyz"}

# ---------------------------------------------------------------------------
# TOKEN REGISTRY
# Contract address is the only reliable identity. Symbols are attacker-controlled.
# Anything not listed here is quarantined, not counted. Add contracts in the UI.
# ---------------------------------------------------------------------------
KNOWN_TOKENS: dict[int, dict[str, tuple[str, float]]] = {
    1: {
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": ("USDC", 1.0),
        "0xdac17f958d2ee523a2206206994597c13d831ec7": ("USDT", 1.0),
        "0x6b175474e89094c44da98b954eedeac495271d0f": ("DAI", 1.0),
        "0x6c3ea9036406852006290770bedfcaba0e23a0e8": ("PYUSD", 1.0),
        "0x4c9edd5852cd905f086c759e8383e09bff1e68b3": ("USDe", 1.0),
        "0x853d955acef822db058eb8505911ed77f175b99e": ("FRAX", 1.0),
        "0x5f98805a4e8be255a32880fdec7f6728c6568ba0": ("LUSD", 1.0),
        "0x0000000000085d4780b73119b644ae5ecd22b376": ("TUSD", 1.0),
        "0x8e870d67f660d95d5be530380d0ec0bd388289e1": ("USDP", 1.0),
    },
}

DEFAULT_TREASURY = "Treasury, 0xB024aC1bC3DBCD3cEc869F77E13F9D7EC00c3A87"
DEFAULT_STRATEGY = ("Strategy 1, 0x2377a430Cc45e6C0Bfe4Aa0783D47FB02f32FC5e\n"
                    "Strategy 2, 0x5e13D8BBA207a9c21BCE74263d8A2079a143440A")

st.set_page_config(page_title="USD Stablecoin Treasury", page_icon="🏦",
                   layout="wide", initial_sidebar_state="collapsed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_usd(x, dp: int = 2) -> str:
    if x is None or pd.isna(x):
        return "-"
    s = f"${abs(x):,.{dp}f}"
    return f"({s})" if x < 0 else s


def clean_symbol(sym: str) -> str:
    """Strip invisible and non-ASCII characters so lookalikes are comparable."""
    norm = unicodedata.normalize("NFKC", sym or "")
    return "".join(c for c in norm if c.isascii() and c.isprintable()).strip()


def looks_like(addr_a: str, addr_b: str) -> bool:
    """Address-poisoning decoys share the first and last few characters."""
    a, b = addr_a.lower(), addr_b.lower()
    if a == b:
        return False
    return a[:6] == b[:6] or a[-4:] == b[-4:]


def resolve_api_key() -> str:
    if os.environ.get("ETHERSCAN_API_KEY"):
        return os.environ["ETHERSCAN_API_KEY"]
    try:
        if "ETHERSCAN_API_KEY" in st.secrets:
            return str(st.secrets["ETHERSCAN_API_KEY"])
    except Exception:
        pass
    return HARDCODED_API_KEY


def parse_wallets(text: str, kind: str) -> list[dict]:
    out = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        label, _, addr = line.partition(",")
        label, addr = label.strip(), addr.strip()
        if not addr:
            label, addr = addr or line[:8], line
        if addr.startswith("0x") and len(addr) == 42:
            out.append({"label": label or addr[:10], "address": addr, "kind": kind})
    return out


class RateLimiter:
    def __init__(self, per_second: float = 4.0):
        self.gap, self.last = 1.0 / per_second, 0.0

    def wait(self):
        d = time.monotonic() - self.last
        if d < self.gap:
            time.sleep(self.gap - d)
        self.last = time.monotonic()


_limiter = RateLimiter()


@st.cache_data(ttl=300, show_spinner=False)
def fetch_transfers(address: str, chain_id: int, api_key: str) -> list[dict]:
    out, seen, start = [], set(), 0
    for _ in range(50):
        _limiter.wait()
        r = requests.get(ETHERSCAN_BASE, params={
            "chainid": chain_id, "module": "account", "action": "tokentx",
            "address": address, "startblock": start, "endblock": 99999999,
            "page": 1, "offset": 10000, "sort": "asc", "apikey": api_key}, timeout=30)
        r.raise_for_status()
        p = r.json()
        if p.get("status") != "1":
            msg = str(p.get("result") or p.get("message") or "")
            if "No transactions found" in msg:
                break
            raise RuntimeError(msg)
        rows = p.get("result") or []
        new = 0
        for t in rows:
            k = (t["hash"], t["from"], t["to"], t["value"], t["contractAddress"])
            if k not in seen:
                seen.add(k)
                out.append(t)
                new += 1
        if len(rows) < 10000 or new == 0:
            break
        start = int(rows[-1]["blockNumber"])
    return out


def demo_transfers(wallets: list[dict]) -> list[dict]:
    rng = random.Random(11)
    tre = [w for w in wallets if w["kind"] == "treasury"]
    strat = [w for w in wallets if w["kind"] == "strategy"]
    if not tre or not strat:
        return []
    usdc = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    usdt = "0xdac17f958d2ee523a2206206994597c13d831ec7"
    funder = "0x9999999999999999999999999999999999999999"
    rows, now, blk = [], datetime.now(timezone.utc), 21_000_000

    def emit(ts, frm, to, ca, sym, amt):
        nonlocal blk
        if ts > now:
            return
        blk += rng.randint(20, 300)
        rows.append({"blockNumber": str(blk), "timeStamp": str(int(ts.timestamp())),
                     "hash": "0x" + f"{rng.getrandbits(256):064x}", "from": frm, "to": to,
                     "contractAddress": ca, "value": str(int(amt * 1e6)),
                     "tokenSymbol": sym, "tokenDecimal": "6"})

    t0 = now - timedelta(days=120)
    emit(t0, funder, tre[0]["address"], usdc, "USDC", 8_000_000)
    emit(t0, funder, tre[0]["address"], usdt, "USDT", 6_000_000)
    for i, s in enumerate(strat):
        ts = t0 + timedelta(days=3 + i * 2)
        for k in range(4):
            ca, sym = (usdc, "USDC") if k % 2 == 0 else (usdt, "USDT")
            amt = rng.choice([500_000, 1_000_000, 1_500_000])
            emit(ts, tre[0]["address"], s["address"], ca, sym, amt)
            ts += timedelta(days=rng.randint(8, 18))
            emit(ts, s["address"], tre[0]["address"], ca, sym, amt * rng.uniform(0.02, 0.1))
            ts += timedelta(days=rng.randint(4, 12))
    return rows


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def build_ledger(raw: dict[str, list[dict]], wallets: list[dict], chain_id: int,
                 extra_ok: set[str]) -> pd.DataFrame:
    index = {w["address"].lower(): w for w in wallets}
    registry = KNOWN_TOKENS.get(chain_id, {})
    known_symbols = {s.upper() for s, _ in registry.values()}
    recs = []

    for addr, rows in raw.items():
        me = index[addr.lower()]
        for t in rows:
            frm, to = t["from"].lower(), t["to"].lower()
            if frm == addr.lower():
                direction, cp = "OUT", to
            elif to == addr.lower():
                direction, cp = "IN", frm
            else:
                continue

            ca = t["contractAddress"].lower()
            raw_sym = t.get("tokenSymbol") or "?"
            sym = clean_symbol(raw_sym) or "?"
            dec = int(t.get("tokenDecimal") or 18)
            amt = int(t["value"]) / (10 ** dec)

            trusted = ca in registry or ca in extra_ok
            reasons = []
            if not trusted:
                if sym.upper() in known_symbols:
                    reasons.append(f"Impersonates {sym.upper()}")
                if raw_sym != sym:
                    reasons.append("Hidden characters in symbol")
                if amt == 0:
                    reasons.append("Zero value")
                if any(looks_like(cp, w["address"]) for w in wallets):
                    reasons.append("Lookalike counterparty")
                if not reasons:
                    reasons.append("Contract not on allowlist")

            label, price = registry.get(ca, (sym, float("nan")))
            cp_w = index.get(cp)
            recs.append({
                "timestamp": datetime.fromtimestamp(int(t["timeStamp"]), tz=timezone.utc),
                "block": int(t["blockNumber"]), "tx_hash": t["hash"],
                "wallet": me["label"], "wallet_kind": me["kind"],
                "wallet_address": me["address"], "direction": direction,
                "asset": label, "raw_symbol": raw_sym, "contract": ca,
                "amount": amt, "signed_amount": amt if direction == "IN" else -amt,
                "price": price, "counterparty": cp,
                "counterparty_kind": cp_w["kind"] if cp_w else "external",
                "counterparty_label": cp_w["label"] if cp_w else cp[:8] + "..." + cp[-4:],
                "trusted": trusted, "flag": "; ".join(reasons),
            })

    df = pd.DataFrame.from_records(recs)
    if df.empty:
        return df
    df["usd"] = df["amount"] * df["price"]
    df["signed_usd"] = df["signed_amount"] * df["price"]
    return df.sort_values("timestamp").reset_index(drop=True)


def qty_at(df: pd.DataFrame, wallet: str, cutoff) -> pd.Series:
    sub = df[(df.wallet == wallet) & (df.timestamp <= cutoff)]
    return sub.groupby("asset")["signed_amount"].sum()


def treasury_composition(df: pd.DataFrame, treasury_labels: list[str],
                         strategy_labels: list[str], opening) -> pd.DataFrame:
    t = df[df.wallet.isin(treasury_labels)]
    live = t[t.timestamp > opening]
    assets = sorted(t["asset"].unique())
    rows = []
    for a in assets:
        ta = t[t.asset == a]
        la = live[live.asset == a]
        opening_qty = ta[ta.timestamp <= opening]["signed_amount"].sum()
        to_strat = -la[(la.direction == "OUT") & (la.counterparty_kind == "strategy")]["signed_amount"].sum()
        from_strat = la[(la.direction == "IN") & (la.counterparty_kind == "strategy")]["signed_amount"].sum()
        ext = la[la.counterparty_kind == "external"]["signed_amount"].sum()
        current = ta["signed_amount"].sum()
        price = ta["price"].dropna().iloc[0] if ta["price"].notna().any() else float("nan")
        rows.append({"Asset": a, "Opening qty": opening_qty, "External funding": ext,
                     "Sent to strategies": to_strat, "Returned from strategies": from_strat,
                     "Current qty": current, "Price": price,
                     "Current value": current * price})
    out = pd.DataFrame(rows)
    tot = out["Current value"].sum()
    out["% of treasury"] = out["Current value"] / tot * 100 if tot else 0.0
    return out


def strategy_table(df: pd.DataFrame, strategy_wallets: list[dict],
                   treasury_labels: list[str]) -> pd.DataFrame:
    rows = []
    for s in strategy_wallets:
        sub = df[df.wallet == s["label"]]
        tre = sub[sub.counterparty_label.isin(treasury_labels)]
        sent = tre.loc[tre.direction == "IN", "usd"].sum()
        ret = tre.loc[tre.direction == "OUT", "usd"].sum()
        ext_out = sub[(sub.counterparty_kind == "external") & (sub.direction == "OUT")]["usd"].sum()
        rows.append({
            "Strategy wallet": s["label"], "Address": s["address"],
            "Transfers": len(sub), "Sent": sent, "Returned": ret,
            "Net deployed": sent - ret,
            "Wallet value": sub["signed_usd"].sum(),
            "Out to venues": ext_out,
            "First funded": tre["timestamp"].min() if not tre.empty else pd.NaT,
            "Last activity": sub["timestamp"].max() if not sub.empty else pd.NaT,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

st.title("USD Stablecoin Treasury Dashboard")

first_run = "init" not in st.session_state
st.session_state["init"] = True

with st.expander("Configuration", expanded=first_run):
    c = st.columns([1, 2, 1, 1])
    chain_name = c[0].selectbox("Chain", list(CHAINS), index=0)
    api_key = c[1].text_input("Etherscan API key", value=resolve_api_key(), type="password")
    opening_date = c[2].date_input("Opening date", value=date(2026, 1, 1),
                                   help="Balances before this date are the opening position.")
    demo = c[3].toggle("Demo data", value=False)

    w = st.columns(2)
    treasury_text = w[0].text_area("Treasury wallets", DEFAULT_TREASURY, height=110,
                                   help="One per line: Label, 0xaddress")
    strategy_text = w[1].text_area("Strategy wallets", DEFAULT_STRATEGY, height=110,
                                   help="One per line: Label, 0xaddress")

    extra_text = st.text_area(
        "Additional trusted token contracts", "", height=68,
        help="One 0x contract per line. Only allowlisted contracts count toward the numbers.")
    if st.button("Refresh"):
        st.cache_data.clear()
        st.rerun()

chain_id = CHAINS[chain_name]
explorer = EXPLORERS[chain_id]
opening_ts = pd.Timestamp(opening_date, tz="UTC")
extra_ok = {l.strip().lower() for l in extra_text.splitlines()
            if l.strip().startswith("0x")}

treasury_wallets = parse_wallets(treasury_text, "treasury")
strategy_wallets = parse_wallets(strategy_text, "strategy")
wallets = treasury_wallets + strategy_wallets
treasury_labels = [w["label"] for w in treasury_wallets]
strategy_labels = [w["label"] for w in strategy_wallets]

if not wallets:
    st.warning("Add at least one treasury and one strategy wallet.")
    st.stop()

# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

raw: dict[str, list[dict]] = {}
if demo:
    rows = demo_transfers(wallets)
    for w in wallets:
        a = w["address"].lower()
        raw[w["address"]] = [r for r in rows
                             if r["from"].lower() == a or r["to"].lower() == a]
else:
    prog = st.progress(0.0, text="Pulling wallet history...")
    for i, w in enumerate(wallets):
        try:
            raw[w["address"]] = fetch_transfers(w["address"], chain_id, api_key)
        except Exception as e:  # noqa: BLE001
            raw[w["address"]] = []
            st.error(f"{w['label']}: {e}")
        prog.progress((i + 1) / len(wallets), text=f"Pulling... {w['label']}")
    prog.empty()

full = build_ledger(raw, wallets, chain_id, extra_ok)
if full.empty:
    st.info(f"No ERC-20 transfers for these wallets on {chain_name}.")
    st.stop()

ledger = full[full.trusted].copy()
quarantine = full[~full.trusted].copy()

if ledger.empty:
    st.error("Every transfer was quarantined. Add the token contract under Configuration.")
    st.dataframe(quarantine[["timestamp", "wallet", "raw_symbol", "contract", "flag"]],
                 hide_index=True, width="stretch")
    st.stop()

comp = treasury_composition(ledger, treasury_labels, strategy_labels, opening_ts)
strats = strategy_table(ledger, strategy_wallets, treasury_labels)

treasury_value = comp["Current value"].sum()
strategy_value = strats["Wallet value"].sum()
total_capital = treasury_value + strategy_value
net_deployed = strats["Net deployed"].sum()
opening_total = ledger[ledger.timestamp <= opening_ts]["signed_usd"].sum()
external_in = ledger[(ledger.counterparty_kind == "external") &
                     (ledger.timestamp > opening_ts)]["signed_usd"].sum()
share = net_deployed / total_capital * 100 if total_capital else float("nan")

st.caption(
    f"{chain_name} · live from {len(wallets)} wallets · {len(ledger)} counted transfers · "
    f"{len(quarantine)} quarantined · opening {opening_ts:%d %b %Y}"
)

if not quarantine.empty:
    spoof = quarantine[quarantine.flag.str.contains("Impersonates|Hidden characters")]
    if not spoof.empty:
        st.error(
            f"{len(spoof)} transfers in {spoof.contract.nunique()} spoofed token contracts "
            f"impersonating a real asset. Excluded from all figures. See the Quarantine tab."
        )

# ---------------------------------------------------------------------------
# Headline
# ---------------------------------------------------------------------------

r1 = st.columns(4)
r1[0].metric("Total capital", fmt_usd(total_capital),
             help="Treasury value plus strategy wallet value")
r1[1].metric("Treasury value", fmt_usd(treasury_value))
r1[2].metric("Strategy wallet value", fmt_usd(strategy_value))
r1[3].metric("Net deployed", fmt_usd(net_deployed), help="Sent to strategies minus returned")

r2 = st.columns(4)
r2[0].metric("Opening capital", fmt_usd(opening_total))
r2[1].metric("External funding since opening", fmt_usd(external_in))
r2[2].metric("Share of capital deployed", f"{share:.1f}%" if pd.notna(share) else "-")
r2[3].metric("Assets held", int((comp["Current qty"] > 0).sum()))

tabs = st.tabs(["Overview", "Treasury", "Strategies", "Strategy detail",
                "Transfer log", "Quarantine"])

# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

with tabs[0]:
    a, b = st.columns([3, 2])

    with a:
        st.subheader("Capital walk")
        sent = strats["Sent"].sum()
        ret = strats["Returned"].sum()
        steps = [("Opening", opening_total, 0, opening_total, "total"),
                 ("External funding", external_in, opening_total,
                  opening_total + external_in, "up"),
                 ("Sent to strategies", -sent, treasury_value, treasury_value + sent, "down"),
                 ("Returned", ret, treasury_value - ret, treasury_value, "up"),
                 ("Treasury now", treasury_value, 0, treasury_value, "total"),
                 ("Strategy wallets", strategy_value, treasury_value, total_capital, "up"),
                 ("Total capital", total_capital, 0, total_capital, "total")]
        walk = pd.DataFrame(steps, columns=["step", "delta", "low", "high", "kind"])
        base = alt.Chart(walk).encode(
            x=alt.X("step:N", sort=list(walk["step"]), title=None,
                    axis=alt.Axis(labelAngle=-25)))
        st.altair_chart(
            (base.mark_bar(size=38).encode(
                y=alt.Y("low:Q", title="USD", axis=alt.Axis(format="$,.2f")), y2="high:Q",
                color=alt.Color("kind:N", scale=alt.Scale(
                    domain=["total", "up", "down"],
                    range=["#4C78A8", "#54A24B", "#E45756"]), legend=None),
                tooltip=[alt.Tooltip("step:N", title=""),
                         alt.Tooltip("delta:Q", format="$,.2f")])
             + base.mark_text(dy=-8, fontSize=10).encode(
                 y="high:Q", text=alt.Text("delta:Q", format="$,.2f"))
             ).properties(height=330),
            use_container_width=True)

    with b:
        st.subheader("Where the capital sits")
        split = pd.DataFrame({
            "bucket": ["Treasury (idle)"] + list(strats["Strategy wallet"]),
            "value": [treasury_value] + list(strats["Wallet value"])})
        split = split[split.value.abs() > 1e-9]
        st.altair_chart(
            alt.Chart(split).mark_arc(innerRadius=60).encode(
                theta="value:Q",
                color=alt.Color("bucket:N", title=None,
                                legend=alt.Legend(orient="bottom", columns=1)),
                tooltip=["bucket", alt.Tooltip("value:Q", format="$,.2f")]
            ).properties(height=330),
            use_container_width=True)

    st.subheader("Cumulative net deployed by strategy wallet")
    tre_flows = ledger[(ledger.wallet_kind == "strategy") &
                       (ledger.counterparty_label.isin(treasury_labels))].copy()
    if not tre_flows.empty:
        tre_flows = tre_flows.sort_values("timestamp")
        tre_flows["cumulative"] = tre_flows.groupby("wallet")["signed_usd"].cumsum()
        st.altair_chart(
            alt.Chart(tre_flows).mark_line(interpolate="step-after", point=True).encode(
                x=alt.X("timestamp:T", title=None),
                y=alt.Y("cumulative:Q", title="Net deployed (USD)",
                        axis=alt.Axis(format="$,.2f")),
                color=alt.Color("wallet:N", title=None, legend=alt.Legend(orient="bottom")),
                tooltip=["wallet", "timestamp:T", "asset",
                         alt.Tooltip("cumulative:Q", format="$,.2f")]
            ).properties(height=280),
            use_container_width=True)

    st.subheader("Net deployed by strategy wallet and asset")
    stack = tre_flows.groupby(["wallet", "asset"], as_index=False)["signed_usd"].sum() \
        if not tre_flows.empty else pd.DataFrame(columns=["wallet", "asset", "signed_usd"])
    if not stack.empty:
        st.altair_chart(
            alt.Chart(stack).mark_bar().encode(
                y=alt.Y("wallet:N", title=None, sort="-x"),
                x=alt.X("signed_usd:Q", title="Net deployed (USD)",
                        axis=alt.Axis(format="$,.2f"), stack="zero"),
                color=alt.Color("asset:N", title="Asset", legend=alt.Legend(orient="bottom")),
                tooltip=["wallet", "asset", alt.Tooltip("signed_usd:Q", format="$,.2f")]
            ).properties(height=220),
            use_container_width=True)

# ---------------------------------------------------------------------------
# Treasury
# ---------------------------------------------------------------------------

with tabs[1]:
    st.subheader("Treasury wallet composition")
    st.caption(" · ".join(f"`{w['address']}`" for w in treasury_wallets))

    show = comp.copy()
    show.loc[len(show)] = {
        "Asset": "Total", "Opening qty": comp["Opening qty"].sum(),
        "External funding": comp["External funding"].sum(),
        "Sent to strategies": comp["Sent to strategies"].sum(),
        "Returned from strategies": comp["Returned from strategies"].sum(),
        "Current qty": comp["Current qty"].sum(), "Price": None,
        "Current value": treasury_value, "% of treasury": 100.0}
    st.dataframe(show, hide_index=True, width="stretch", column_config={
        c: st.column_config.NumberColumn(format="%,.4f") for c in
        ["Opening qty", "External funding", "Sent to strategies",
         "Returned from strategies", "Current qty"]} | {
        "Price": st.column_config.NumberColumn(format="%.4f"),
        "Current value": st.column_config.NumberColumn(format="$%,.2f"),
        "% of treasury": st.column_config.NumberColumn(format="%.1f%%")})
    st.caption("Current qty = opening + external funding - sent + returned. Ties by construction.")

    x, y = st.columns(2)
    with x:
        st.subheader("Composition by asset")
        st.altair_chart(
            alt.Chart(comp).mark_bar().encode(
                y=alt.Y("Asset:N", title=None, sort="-x"),
                x=alt.X("Current value:Q", title="USD", axis=alt.Axis(format="$,.2f")),
                tooltip=["Asset", alt.Tooltip("Current value:Q", format="$,.2f"),
                         alt.Tooltip("% of treasury:Q", format=".1f")]
            ).properties(height=240),
            use_container_width=True)
    with y:
        st.subheader("Treasury balance over time")
        tr = ledger[ledger.wallet.isin(treasury_labels)].sort_values("timestamp").copy()
        tr["balance"] = tr["signed_usd"].cumsum()
        st.altair_chart(
            alt.Chart(tr).mark_area(interpolate="step-after", opacity=0.3, line=True).encode(
                x=alt.X("timestamp:T", title=None),
                y=alt.Y("balance:Q", title="USD", axis=alt.Axis(format="$,.2f")),
                tooltip=["timestamp:T", "asset", alt.Tooltip("balance:Q", format="$,.2f")]
            ).properties(height=240),
            use_container_width=True)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

with tabs[2]:
    st.subheader("Strategy wallets")
    st.caption("Click a row to open it in Strategy detail.")

    table = strats[["Strategy wallet", "Transfers", "Sent", "Returned", "Net deployed",
                    "Wallet value", "Out to venues", "First funded", "Last activity"]]
    event = st.dataframe(
        table, hide_index=True, width="stretch",
        on_select="rerun", selection_mode="single-row",
        column_config={c: st.column_config.NumberColumn(format="$%,.2f") for c in
                       ["Sent", "Returned", "Net deployed", "Wallet value", "Out to venues"]})
    if event.selection.rows:
        st.session_state["sel"] = strats.iloc[event.selection.rows[0]]["Strategy wallet"]
        st.info(f"Selected **{st.session_state['sel']}** — open the Strategy detail tab.")

    st.subheader("Net deployed versus wallet value")
    cmp_df = strats.melt(id_vars="Strategy wallet",
                         value_vars=["Net deployed", "Wallet value"],
                         var_name="basis", value_name="usd")
    st.altair_chart(
        alt.Chart(cmp_df).mark_bar().encode(
            x=alt.X("Strategy wallet:N", title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("usd:Q", title="USD", axis=alt.Axis(format="$,.2f")),
            xOffset="basis:N",
            color=alt.Color("basis:N", title=None, legend=alt.Legend(orient="bottom")),
            tooltip=["Strategy wallet", "basis", alt.Tooltip("usd:Q", format="$,.2f")]
        ).properties(height=260),
        use_container_width=True)
    st.caption("A gap means capital has left the wallet for a venue, or value arrived from one.")

    st.subheader("Net deployed by asset (USD)")
    if not tre_flows.empty:
        m = tre_flows.pivot_table(index="wallet", columns="asset",
                                  values="signed_usd", aggfunc="sum", fill_value=0.0)
        m["Total"] = m.sum(axis=1)
        m.loc["Total"] = m.sum()
        m.index.name = "Strategy wallet"
        st.dataframe(m.style.format(lambda v: fmt_usd(v) if v else "-"), width="stretch")

# ---------------------------------------------------------------------------
# Strategy detail
# ---------------------------------------------------------------------------

with tabs[3]:
    opts = list(strats["Strategy wallet"])
    idx = opts.index(st.session_state["sel"]) if st.session_state.get("sel") in opts else 0
    chosen = st.selectbox("Strategy wallet", opts, index=idx)
    row = strats[strats["Strategy wallet"] == chosen].iloc[0]
    st.markdown(f"`{row['Address']}` · [explorer]({explorer}/address/{row['Address']})")

    m = st.columns(5)
    m[0].metric("Net deployed", fmt_usd(row["Net deployed"]))
    m[1].metric("Wallet value", fmt_usd(row["Wallet value"]))
    m[2].metric("Sent", fmt_usd(row["Sent"]))
    m[3].metric("Returned", fmt_usd(row["Returned"]))
    m[4].metric("Transfers", int(row["Transfers"]))

    sub = ledger[ledger.wallet == chosen]
    st.subheader("Net position by asset")
    pos = []
    for a in sorted(sub["asset"].unique()):
        sa = sub[(sub.asset == a) & (sub.counterparty_label.isin(treasury_labels))]
        sq = sa.loc[sa.direction == "IN", "amount"].sum()
        rq = sa.loc[sa.direction == "OUT", "amount"].sum()
        su = sa.loc[sa.direction == "IN", "usd"].sum()
        ru = sa.loc[sa.direction == "OUT", "usd"].sum()
        pos.append({"Asset": a, "Sent (tokens)": sq, "Returned (tokens)": rq,
                    "Net (tokens)": sq - rq, "Sent (USD)": su, "Returned (USD)": ru,
                    "Net (USD)": su - ru,
                    "Wallet balance (tokens)": sub[sub.asset == a]["signed_amount"].sum()})
    st.dataframe(pd.DataFrame(pos), hide_index=True, width="stretch", column_config={
        c: st.column_config.NumberColumn(format="%,.4f") for c in
        ["Sent (tokens)", "Returned (tokens)", "Net (tokens)", "Wallet balance (tokens)"]} | {
        c: st.column_config.NumberColumn(format="$%,.2f") for c in
        ["Sent (USD)", "Returned (USD)", "Net (USD)"]})

    st.subheader("Transfers to and from treasury")
    det = sub[sub.counterparty_label.isin(treasury_labels)].copy()
    det["Explorer"] = det["tx_hash"].map(lambda h: f"{explorer}/tx/{h}")
    det["Direction"] = det["direction"].map({"IN": "Out to strategy", "OUT": "Return to treasury"})
    st.dataframe(
        det[["timestamp", "Direction", "asset", "amount", "usd", "counterparty_label",
             "tx_hash", "Explorer"]]
        .sort_values("timestamp", ascending=False)
        .rename(columns={"timestamp": "Time (UTC)", "asset": "Asset",
                         "amount": "Amount (tokens)", "usd": "USD value",
                         "counterparty_label": "Treasury wallet", "tx_hash": "Tx"}),
        hide_index=True, width="stretch", column_config={
            "Amount (tokens)": st.column_config.NumberColumn(format="%,.4f"),
            "USD value": st.column_config.NumberColumn(format="$%,.2f"),
            "Explorer": st.column_config.LinkColumn(display_text="view")})

    st.subheader("All wallet activity")
    allv = sub.copy()
    allv["Explorer"] = allv["tx_hash"].map(lambda h: f"{explorer}/tx/{h}")
    st.dataframe(
        allv[["timestamp", "direction", "asset", "amount", "usd", "counterparty_kind",
              "counterparty_label", "Explorer"]]
        .sort_values("timestamp", ascending=False)
        .rename(columns={"timestamp": "Time (UTC)", "direction": "Dir", "asset": "Asset",
                         "amount": "Amount", "usd": "USD", "counterparty_kind": "CP type",
                         "counterparty_label": "Counterparty"}),
        hide_index=True, width="stretch", column_config={
            "Amount": st.column_config.NumberColumn(format="%,.4f"),
            "USD": st.column_config.NumberColumn(format="$%,.2f"),
            "Explorer": st.column_config.LinkColumn(display_text="view")})

    st.download_button("Download wallet ledger (CSV)", allv.to_csv(index=False).encode(),
                       file_name=f"{chosen.replace(' ', '_')}.csv", mime="text/csv")

# ---------------------------------------------------------------------------
# Transfer log
# ---------------------------------------------------------------------------

with tabs[4]:
    f = st.columns(4)
    fw = f[0].multiselect("Wallet", sorted(ledger["wallet"].unique()))
    fa = f[1].multiselect("Asset", sorted(ledger["asset"].unique()))
    fd = f[2].multiselect("Direction", ["IN", "OUT"])
    fc = f[3].multiselect("Counterparty type", ["treasury", "strategy", "external"])

    log = ledger.copy()
    for col, sel in [("wallet", fw), ("asset", fa), ("direction", fd),
                     ("counterparty_kind", fc)]:
        if sel:
            log = log[log[col].isin(sel)]

    k = st.columns(4)
    k[0].metric("Transfers", len(log))
    k[1].metric("In", fmt_usd(log.loc[log.direction == "IN", "usd"].sum()))
    k[2].metric("Out", fmt_usd(log.loc[log.direction == "OUT", "usd"].sum()))
    k[3].metric("Net", fmt_usd(log["signed_usd"].sum()))

    log = log.copy()
    log["Explorer"] = log["tx_hash"].map(lambda h: f"{explorer}/tx/{h}")
    st.dataframe(
        log[["timestamp", "wallet", "direction", "asset", "amount", "usd",
             "counterparty_kind", "counterparty_label", "block", "Explorer"]]
        .sort_values("timestamp", ascending=False)
        .rename(columns={"timestamp": "Time (UTC)", "wallet": "Wallet", "direction": "Dir",
                         "asset": "Asset", "amount": "Amount", "usd": "USD",
                         "counterparty_kind": "CP type", "counterparty_label": "Counterparty",
                         "block": "Block"}),
        hide_index=True, width="stretch", column_config={
            "Amount": st.column_config.NumberColumn(format="%,.4f"),
            "USD": st.column_config.NumberColumn(format="$%,.2f"),
            "Explorer": st.column_config.LinkColumn(display_text="view")})
    st.download_button("Download transfer log (CSV)", log.to_csv(index=False).encode(),
                       file_name="transfer_log.csv", mime="text/csv")

# ---------------------------------------------------------------------------
# Quarantine
# ---------------------------------------------------------------------------

with tabs[5]:
    st.subheader("Quarantined transfers")
    st.caption(
        "Excluded from every figure. Token identity is the contract address, never the "
        "symbol: a symbol can contain invisible characters and lookalike glyphs. "
        "To count a contract, add it under Configuration."
    )
    if quarantine.empty:
        st.success("Nothing quarantined.")
    else:
        bycontract = (quarantine.groupby(["contract", "raw_symbol", "flag"])
                      .agg(transfers=("amount", "size"), tokens=("amount", "sum"),
                           first_seen=("timestamp", "min"), last_seen=("timestamp", "max"))
                      .reset_index())
        bycontract["codepoints"] = bycontract["raw_symbol"].map(
            lambda s: " ".join(f"U+{ord(c):04X}" for c in s))
        bycontract["Explorer"] = bycontract["contract"].map(
            lambda a: f"{explorer}/token/{a}")
        st.dataframe(bycontract.rename(columns={
            "contract": "Contract", "raw_symbol": "Symbol as reported", "flag": "Why",
            "transfers": "Transfers", "tokens": "Tokens", "first_seen": "First seen",
            "last_seen": "Last seen", "codepoints": "Symbol codepoints"}),
            hide_index=True, width="stretch",
            column_config={"Explorer": st.column_config.LinkColumn(display_text="view"),
                           "Tokens": st.column_config.NumberColumn(format="%,.4f")})

        st.subheader("Individual transfers")
        q = quarantine.copy()
        q["Explorer"] = q["tx_hash"].map(lambda h: f"{explorer}/tx/{h}")
        st.dataframe(
            q[["timestamp", "wallet", "direction", "raw_symbol", "amount",
               "counterparty", "contract", "flag", "Explorer"]]
            .sort_values("timestamp", ascending=False)
            .rename(columns={"timestamp": "Time (UTC)", "wallet": "Wallet",
                             "direction": "Dir", "raw_symbol": "Symbol", "amount": "Amount",
                             "counterparty": "Counterparty", "contract": "Contract",
                             "flag": "Why"}),
            hide_index=True, width="stretch",
            column_config={"Amount": st.column_config.NumberColumn(format="%,.4f"),
                           "Explorer": st.column_config.LinkColumn(display_text="view")})
