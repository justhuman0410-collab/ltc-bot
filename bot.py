import os
import json
import time
import threading
import requests
import discord
from discord import app_commands, Embed
from discord.ext import tasks
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
USER_ID = int(os.getenv("USER_ID"))
LTC_ADDRESS = os.getenv("LTC_ADDRESS")
CHECK_SECONDS = int(os.getenv("CHECK_SECONDS", "15"))
MAX_CONFIRMATIONS_ALERT = int(os.getenv("MAX_CONFIRMATIONS_ALERT", "12"))
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "5000"))

STATE_FILE = "bot_state.json"
BLOCKCYPHER_API = f"https://api.blockcypher.com/v1/ltc/main/addrs/{LTC_ADDRESS}/full?limit=10"

intents = discord.Intents.default()

price_cache = {"value": 0, "last_update": 0}
dashboard_data = {
    "address": LTC_ADDRESS,
    "balance_ltc": 0,
    "balance_usd": 0,
    "ltc_price": 0,
    "total_received": 0,
    "total_sent": 0,
    "last_check": "Never",
    "recent_txs": []
}


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"seen_txs": {}, "first_run": True}

    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {"seen_txs": {}, "first_run": True}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=4)


state = load_state()


def ltc_from_satoshi(value):
    return value / 100_000_000


def get_ltc_price_usd():
    if time.time() - price_cache["last_update"] < 60 and price_cache["value"] > 0:
        return price_cache["value"]

    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {"ids": "litecoin", "vs_currencies": "usd"}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        price = r.json()["litecoin"]["usd"]

        price_cache["value"] = price
        price_cache["last_update"] = time.time()
        return price
    except:
        return price_cache["value"]


def get_wallet_data():
    r = requests.get(BLOCKCYPHER_API, timeout=20)
    r.raise_for_status()
    return r.json()


def get_received_amount_for_address(tx):
    total = 0
    for output in tx.get("outputs", []):
        if LTC_ADDRESS in output.get("addresses", []):
            total += output.get("value", 0)
    return ltc_from_satoshi(total)


def get_sent_amount_from_address(tx):
    total = 0
    for inp in tx.get("inputs", []):
        if LTC_ADDRESS in inp.get("addresses", []):
            total += inp.get("output_value", 0)
    return ltc_from_satoshi(total)


def tx_link(tx_hash):
    return f"https://live.blockcypher.com/ltc/tx/{tx_hash}/"


def update_dashboard_data(data):
    price = get_ltc_price_usd()
    balance_ltc = ltc_from_satoshi(data.get("balance", 0))
    total_received = ltc_from_satoshi(data.get("total_received", 0))
    total_sent = ltc_from_satoshi(data.get("total_sent", 0))

    recent = []
    for tx in data.get("txs", [])[:10]:
        tx_hash = tx.get("hash", "")
        received = get_received_amount_for_address(tx)
        sent = get_sent_amount_from_address(tx)
        confirmations = tx.get("confirmations", 0)

        if received > 0:
            direction = "Received"
            amount = received
        elif sent > 0:
            direction = "Sent"
            amount = sent
        else:
            direction = "Other"
            amount = 0

        recent.append({
            "hash": tx_hash,
            "short_hash": tx_hash[:14] + "...",
            "direction": direction,
            "amount_ltc": amount,
            "amount_usd": amount * price,
            "confirmations": confirmations,
            "link": tx_link(tx_hash)
        })

    dashboard_data.update({
        "address": LTC_ADDRESS,
        "balance_ltc": balance_ltc,
        "balance_usd": balance_ltc * price,
        "ltc_price": price,
        "total_received": total_received,
        "total_sent": total_sent,
        "last_check": time.strftime("%Y-%m-%d %H:%M:%S"),
        "recent_txs": recent
    })


class LTCNotifier(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        check_wallet.change_interval(seconds=CHECK_SECONDS)
        check_wallet.start()


client = LTCNotifier()


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    print("LTC notifier is running.")
    print(f"Dashboard: http://127.0.0.1:{DASHBOARD_PORT}")


@client.tree.command(name="addy", description="Show LTC wallet address")
async def addy(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"📬 **Your LTC Address:**\n`{LTC_ADDRESS}`",
        ephemeral=True
    )


@client.tree.command(name="balance", description="Show LTC wallet balance")
async def balance(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        data = get_wallet_data()
        update_dashboard_data(data)

        embed = Embed(title="💰 LTC Wallet Balance", color=0x00ff99)
        embed.add_field(
            name="Current Balance",
            value=f"**{dashboard_data['balance_ltc']:.8f} LTC**\n≈ **${dashboard_data['balance_usd']:.2f} USD**",
            inline=False
        )
        embed.add_field(name="LTC Price", value=f"${dashboard_data['ltc_price']:.2f}", inline=True)
        embed.add_field(name="Total Received", value=f"{dashboard_data['total_received']:.8f} LTC", inline=True)
        embed.add_field(name="Total Sent", value=f"{dashboard_data['total_sent']:.8f} LTC", inline=True)
        embed.add_field(name="Dashboard", value=f"http://127.0.0.1:{DASHBOARD_PORT}", inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Error:\n`{e}`", ephemeral=True)


@client.tree.command(name="history", description="Show recent LTC transactions")
async def history(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        data = get_wallet_data()
        update_dashboard_data(data)

        if not dashboard_data["recent_txs"]:
            await interaction.followup.send("No recent transactions found.", ephemeral=True)
            return

        embed = Embed(title="📜 Recent LTC Transactions", color=0x3399ff)

        for tx in dashboard_data["recent_txs"][:5]:
            embed.add_field(
                name=f"{tx['direction']} | {tx['confirmations']} conf",
                value=(
                    f"Amount: `{tx['amount_ltc']:.8f} LTC` ≈ `${tx['amount_usd']:.2f}`\n"
                    f"TX: [`{tx['short_hash']}`]({tx['link']})"
                ),
                inline=False
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Error:\n`{e}`", ephemeral=True)


@client.tree.command(name="status", description="Show bot status")
async def status(interaction: discord.Interaction):
    embed = Embed(title="✅ LTC Notifier Status", color=0x00ff99)
    embed.add_field(name="Check Interval", value=f"{CHECK_SECONDS} seconds", inline=True)
    embed.add_field(name="Max Confirmation Alerts", value=str(MAX_CONFIRMATIONS_ALERT), inline=True)
    embed.add_field(name="Tracked TXs", value=str(len(state.get("seen_txs", {}))), inline=True)
    embed.add_field(name="Last Check", value=dashboard_data["last_check"], inline=False)
    embed.add_field(name="Dashboard", value=f"http://127.0.0.1:{DASHBOARD_PORT}", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tasks.loop(seconds=15)
async def check_wallet():
    try:
        data = get_wallet_data()
        update_dashboard_data(data)

        user = await client.fetch_user(USER_ID)
        seen_txs = state["seen_txs"]
        ltc_price = get_ltc_price_usd()

        for tx in reversed(data.get("txs", [])):
            tx_hash = tx.get("hash")
            confirmations = tx.get("confirmations", 0)

            if not tx_hash:
                continue

            received_amount = get_received_amount_for_address(tx)

            if received_amount <= 0:
                continue

            received_usd = received_amount * ltc_price

            if tx_hash not in seen_txs:
                seen_txs[tx_hash] = confirmations
                save_state(state)

                if state.get("first_run", True):
                    continue

                embed = Embed(
                    title="💰 LTC Received!",
                    description="New incoming Litecoin transaction detected.",
                    color=0x00ff99
                )
                embed.add_field(
                    name="Amount",
                    value=f"**{received_amount:.8f} LTC**\n≈ **${received_usd:.2f} USD**",
                    inline=False
                )
                embed.add_field(name="Confirmations", value=str(confirmations), inline=True)
                embed.add_field(name="TX Hash", value=f"`{tx_hash}`", inline=False)
                embed.add_field(name="TX Link", value=tx_link(tx_hash), inline=False)

                await user.send(embed=embed)

            else:
                old_confirmations = seen_txs[tx_hash]

                if confirmations > old_confirmations:
                    seen_txs[tx_hash] = confirmations
                    save_state(state)

                    if confirmations <= MAX_CONFIRMATIONS_ALERT:
                        embed = Embed(title="🔔 LTC Confirmation Update", color=0x3399ff)
                        embed.add_field(
                            name="Amount",
                            value=f"{received_amount:.8f} LTC\n≈ ${received_usd:.2f} USD",
                            inline=False
                        )
                        embed.add_field(name="Confirmations", value=f"**{confirmations}**", inline=True)
                        embed.add_field(name="TX Hash", value=f"`{tx_hash}`", inline=False)
                        embed.add_field(name="TX Link", value=tx_link(tx_hash), inline=False)

                        await user.send(embed=embed)

        if state.get("first_run", True):
            state["first_run"] = False
            save_state(state)

    except Exception as e:
        print("Wallet check error:", e)


app = Flask(__name__)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>LTC Dashboard</title>
    <meta http-equiv="refresh" content="10">
    <style>
        body {
            background: #0f172a;
            color: white;
            font-family: Arial, sans-serif;
            padding: 30px;
        }
        .card {
            background: #111827;
            border-radius: 14px;
            padding: 20px;
            margin-bottom: 20px;
            box-shadow: 0 0 20px rgba(0,0,0,0.3);
        }
        .title {
            font-size: 28px;
            font-weight: bold;
            margin-bottom: 10px;
        }
        .value {
            font-size: 24px;
            color: #22c55e;
            font-weight: bold;
        }
        .small {
            color: #9ca3af;
            font-size: 14px;
            word-break: break-all;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 15px;
        }
        th, td {
            padding: 12px;
            border-bottom: 1px solid #374151;
            text-align: left;
        }
        a {
            color: #60a5fa;
        }
        .received {
            color: #22c55e;
        }
        .sent {
            color: #ef4444;
        }
    </style>
</head>
<body>
    <div class="title">💰 LTC Wallet Dashboard</div>

    <div class="card">
        <div class="small">Wallet Address</div>
        <div class="small">{{ data.address }}</div>
    </div>

    <div class="card">
        <div class="small">Current Balance</div>
        <div class="value">{{ "%.8f"|format(data.balance_ltc) }} LTC</div>
        <div class="value">${{ "%.2f"|format(data.balance_usd) }} USD</div>
    </div>

    <div class="card">
        <div class="small">LTC Price</div>
        <div class="value">${{ "%.2f"|format(data.ltc_price) }}</div>
    </div>

    <div class="card">
        <div class="small">Total Received</div>
        <div>{{ "%.8f"|format(data.total_received) }} LTC</div>
        <br>
        <div class="small">Total Sent</div>
        <div>{{ "%.8f"|format(data.total_sent) }} LTC</div>
        <br>
        <div class="small">Last Check: {{ data.last_check }}</div>
    </div>

    <div class="card">
        <div class="title">Recent Transactions</div>
        <table>
            <tr>
                <th>Type</th>
                <th>Amount</th>
                <th>USD</th>
                <th>Confirmations</th>
                <th>TX</th>
            </tr>
            {% for tx in data.recent_txs %}
            <tr>
                <td class="{{ 'received' if tx.direction == 'Received' else 'sent' }}">{{ tx.direction }}</td>
                <td>{{ "%.8f"|format(tx.amount_ltc) }} LTC</td>
                <td>${{ "%.2f"|format(tx.amount_usd) }}</td>
                <td>{{ tx.confirmations }}</td>
                <td><a href="{{ tx.link }}" target="_blank">{{ tx.short_hash }}</a></td>
            </tr>
            {% endfor %}
        </table>
    </div>
</body>
</html>
"""


@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML, data=dashboard_data)


@app.route("/api")
def api_data():
    return jsonify(dashboard_data)


def run_dashboard():
    app.run(host="127.0.0.1", port=DASHBOARD_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    print(f"Starting dashboard on port {PORT}")
    threading.Thread(target=run_dashboard, daemon=False).start()
    client.run(DISCORD_TOKEN)
