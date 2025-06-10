from flask import Flask, render_template_string, request
import requests
import pandas as pd
import datetime
import time
from collections import deque

app = Flask(__name__)

# Rolling buffer for OI and volume history per strike
# strike -> deque of (timestamp, ce_oi_chg, ce_vol_chg, pe_oi_chg, pe_vol_chg)
history = {}
window_minutes = 20

# HTML template including new key levels and strength indicator
template = '''
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>NSE Option Chain Live</title>
    <meta http-equiv="refresh" content="60">
    <style>
      table { border-collapse: collapse; width: 90%; margin: 20px auto; font-family: Arial, sans-serif; }
      th, td { border: 1px solid #ccc; padding: 6px; text-align: center; }
      th { background-color: rgba(255, 255, 0, 0.2); }
      .atm { background-color: rgba(255, 165, 0, 0.2); }
      .max-ce { background-color: rgba(0, 128, 0, 0.2); }
      .max-pe { background-color: rgba(255, 0, 0, 0.2); }
      .strike-cell, .strike-header { background-color: rgba(173, 216, 230, 0.2); }
      form { text-align: center; margin-top: 20px; }
      .info { text-align: left; margin: 8px 5%; font-size: 1em; font-weight: bold; }
      .timestamp { text-align: center; font-size: 0.9em; margin-top: 10px; }
      .stats { position: fixed; top: 20px; right: 20px; font-size: 0.8em; background: #f9f9f9; padding: 8px; border: 1px solid #ddd; border-radius: 4px; }
    </style>
  </head>
  <body>
    <form method="post">
      Symbol: <input name="symbol" value="{{ symbol }}" required>
      Expiry: <select name="expiry">{% for e in expiries %}<option value="{{ e }}" {% if e==expiry %}selected{% endif %}>{{ e }}</option>{% endfor %}</select>
      <button type="submit">Update</button>
    </form>
    <div class="timestamp">Last Update: {{ timestamp }}</div>
    <div class="info">Current ATM Strike: {{ atm }}</div>
    <div class="info">Market shifting toward strike {{ shift_strike }} based on OI build-up |  Underlying Price: {{ underlying }}</div>
    <div class="info">CE Signal: {{ ce_signal }} (Confirmation {{ confirm_pct }}%) | PE Signal: {{ pe_signal }}</div>
    <div class="stats">
      <strong>Max CE OI:</strong> {{ max_ce_oi.strike }} ({{ max_ce_oi.value }})<br>
      <strong>Max PE OI:</strong> {{ max_pe_oi.strike }} ({{ max_pe_oi.value }})<br>
      <strong>Max CE Vol:</strong> {{ max_ce_vol.strike }} ({{ max_ce_vol.value }})<br>
      <strong>Max PE Vol:</strong> {{ max_pe_vol.strike }} ({{ max_pe_vol.value }})
    </div>
    <!-- New hard-to-break levels -->
    <div class="info">
      Key CE levels (hard to break): {{ key_ce_levels[0] }}, {{ key_ce_levels[1] }}<br>
      Key PE levels (hard to break): {{ key_pe_levels[0] }}, {{ key_pe_levels[1] }}
    </div>
    <!-- Strength indicator -->
    <div class="info">
      Option pressure: <strong>{{ strength_indicator }}</strong>
    </div>
    <table>
      <tr><th>CE OI</th><th>&#x394;CE OI</th><th>CE Vol</th><th>&#x394;CE Vol</th><th>CE LTP</th><th>&#x394;CE LTP%</th>
      <th class="strike-header">Strike</th><th>&#x394;PE LTP%</th><th>PE LTP</th><th>&#x394;PE Vol</th><th>PE Vol</th><th>&#x394;PE OI</th><th>PE OI</th></tr>
      {% for r in rows %}
      <tr class="{% if r.strike==atm %}atm{% endif %}">
        <td>{{ r.ce_oi }}</td><td class="{% if r.ce_oi_chg==max_ce_chg %}max-ce{% endif %}">{{ r.ce_oi_chg }}</td>
        <td>{{ r.ce_vol }}</td><td class="{% if r.ce_vol_chg==max_ce_vol_chg %}max-ce{% endif %}">{{ r.ce_vol_chg }}</td>
        <td>{{ r.ce_ltp }}</td><td>{{ r.ce_ltp_chg }}</td>
        <td class="strike-cell">{{ r.strike }}</td>
        <td>{{ r.pe_ltp_chg }}</td><td>{{ r.pe_ltp }}</td>
        <td class="{% if r.pe_vol_chg==max_pe_vol_chg %}max-pe{% endif %}">{{ r.pe_vol_chg }}</td>
        <td>{{ r.pe_vol }}</td><td class="{% if r.pe_oi_chg==max_pe_chg %}max-pe{% endif %}">{{ r.pe_oi_chg }}</td><td>{{ r.pe_oi }}</td>
      </tr>
      {% endfor %}
    </table>
  </body>
</html>'''

# Backtest and trackers
tests = []
horizon = datetime.timedelta(minutes=5)
prev_vol = {}
prev_ltp = {}


def get_chain(symbol):
    url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*", "Referer": "https://www.nseindia.com/option-chain"}
    sess = requests.Session()
    sess.headers.update(headers)
    sess.get("https://www.nseindia.com", timeout=5)
    time.sleep(1)
    sess.get("https://www.nseindia.com/option-chain", timeout=5)
    time.sleep(1)
    return sess.get(url, timeout=5).json()


def nearest_expiry(data):
    dates = data['records']['expiryDates']
    today = datetime.date.today()
    dts = [datetime.datetime.strptime(x, "%d-%b-%Y").date() for x in dates]
    future = [d for d in dts if d >= today]
    return min(future).strftime("%d-%b-%Y")


def build_df(data, expiry):
    rows = []
    for r in data['records']['data']:
        if r['expiryDate'] == expiry:
            strike = r['strikePrice']
            ce = r.get('CE', {})
            pe = r.get('PE', {})
            # compute CE changes
            ce_vol = ce.get('totalTradedVolume', 0)
            prev_ce_vol = prev_vol.get(('CE', strike), ce_vol)
            ce_vol_chg = ce_vol - prev_ce_vol
            ce_oi = ce.get('openInterest', 0)
            ce_oi_chg = ce.get('changeinOpenInterest', 0)
            ce_ltp = ce.get('lastPrice', 0)
            ce_change = ce.get('change', 0)
            prev_ce_ltp = prev_ltp.get(('CE', strike), ce_ltp - ce_change)
            ce_ltp_chg = f"{round((ce_change / prev_ce_ltp * 100), 2) if prev_ce_ltp else 0}%"
            # compute PE changes
            pe_vol = pe.get('totalTradedVolume', 0)
            prev_pe_vol = prev_vol.get(('PE', strike), pe_vol)
            pe_vol_chg = pe_vol - prev_pe_vol
            pe_oi = pe.get('openInterest', 0)
            pe_oi_chg = pe.get('changeinOpenInterest', 0)
            pe_ltp = pe.get('lastPrice', 0)
            pe_change = pe.get('change', 0)
            prev_pe_ltp = prev_ltp.get(('PE', strike), pe_ltp - pe_change)
            pe_ltp_chg = f"{round((pe_change / prev_pe_ltp * 100), 2) if prev_pe_ltp else 0}%"
            # update trackers
            prev_vol[('CE', strike)] = ce_vol
            prev_vol[('PE', strike)] = pe_vol
            prev_ltp[('CE', strike)] = ce_ltp - ce_change
            prev_ltp[('PE', strike)] = pe_ltp - pe_change
            rows.append({
                'strike': strike,
                'ce_oi': ce_oi,
                'ce_oi_chg': ce_oi_chg,
                'ce_vol': ce_vol,
                'ce_vol_chg': ce_vol_chg,
                'ce_ltp': ce_ltp,
                'ce_ltp_chg': ce_ltp_chg,
                'pe_oi': pe_oi,
                'pe_oi_chg': pe_oi_chg,
                'pe_vol': pe_vol,
                'pe_vol_chg': pe_vol_chg,
                'pe_ltp': pe_ltp,
                'pe_ltp_chg': pe_ltp_chg
            })
    return pd.DataFrame(rows).sort_values('strike').reset_index(drop=True)


def slice_df(df, atm, width=10):
    strikes = df['strike'].tolist()
    if atm not in strikes:
        atm = min(strikes, key=lambda x: abs(x - atm))
    idx = strikes.index(atm)
    return df.loc[max(idx-width, 0): idx+width+1].reset_index(drop=True), atm


def prune_history():
    cutoff = datetime.datetime.now() - datetime.timedelta(minutes=window_minutes)
    for dq in history.values():
        while dq and dq[0][0] < cutoff:
            dq.popleft()

@app.route('/', methods=['GET','POST'])
def index():
    symbol = request.form.get('symbol', 'NIFTY').upper()
    data = get_chain(symbol)
    expiry = request.form.get('expiry') or nearest_expiry(data)
    df = build_df(data, expiry)
    uv = data['records']['underlyingValue']
    sl, atm = slice_df(df, int(round(uv)))

    now = datetime.datetime.now()
    # record history
    for r in sl.to_dict('records'):
        strike = r['strike']
        history.setdefault(strike, deque())
        history[strike].append((now, r['ce_oi_chg'], r['ce_vol_chg'], r['pe_oi_chg'], r['pe_vol_chg']))
    prune_history()

    # compute momentum scores
    ce_scores = {s: sum(d[1] for d in dq if d[1]>0) for s, dq in history.items()}
    pe_scores = {s: sum(d[4] for d in dq if d[4]>0) for s, dq in history.items()}
    key_ce_levels = sorted(ce_scores, key=ce_scores.get, reverse=True)[:2]
    key_pe_levels = sorted(pe_scores, key=pe_scores.get, reverse=True)[:2]

    # determine strength: compare aggregated CE vs PE activity at key levels
    ce_activity = sum(ce_scores.get(s, 0) for s in key_ce_levels)
    pe_activity = sum(pe_scores.get(s, 0) for s in key_pe_levels)
    if ce_activity > pe_activity * 1.1:
        strength_indicator = 'CE Strong, PE Weak'
    elif pe_activity > ce_activity * 1.1:
        strength_indicator = 'PE Strong, CE Weak'
    else:
        strength_indicator = 'Balanced CE/PE'

    # existing signals
    max_ce_chg = sl['ce_oi_chg'].max()
    max_pe_chg = sl['pe_oi_chg'].max()
    ce_strike = int(sl.loc[sl['ce_oi_chg'].idxmax()]['strike'])
    pe_strike = int(sl.loc[sl['pe_oi_chg'].idxmax()]['strike'])
    ce_signal = f"BUY CE @{ce_strike}" if max_ce_chg>0 else "NEUTRAL"
    pe_signal = f"BUY PE @{pe_strike}" if max_pe_chg>0 else "NEUTRAL"

    total_mom = sl['ce_oi_chg'] + sl['pe_oi_chg']
    denom = total_mom.sum() if total_mom.sum()>0 else 1
    shift_strike = int((sl['strike']*total_mom).sum()/denom)

    # backtest confirmation
    tests.append((ce_signal, now, uv))
    cutoff2 = now - horizon
    tests[:] = [t for t in tests if t[1] >= cutoff2]
    hits = sum(1 for sig, t0, uv0 in tests if ((sig.startswith('BUY CE') and uv > uv0) or (not sig.startswith('BUY CE') and uv <= uv0)))
    confirm_pct = round(hits/len(tests)*100,2) if tests else 0

    context = {
        'symbol': symbol,
        'expiries': data['records']['expiryDates'],
        'expiry': expiry,
        'rows': sl.to_dict('records'),
        'atm': atm,
        'underlying': uv,
        'timestamp': now.strftime('%Y-%m-%d %H:%M:%S'),
        'max_ce_oi': {'strike': ce_strike, 'value': int(max_ce_chg)},
        'max_pe_oi': {'strike': pe_strike, 'value': int(max_pe_chg)},
        'max_ce_vol': {'strike': 0, 'value': 0},
        'max_pe_vol': {'strike': 0, 'value': 0},
        'max_ce_chg': max_ce_chg,
        'max_pe_chg': max_pe_chg,
        'max_ce_vol_chg':0,
        'max_pe_vol_chg':0,
        'ce_signal': ce_signal,
        'pe_signal': pe_signal,
        'confirm_pct': confirm_pct,
        'shift_strike': shift_strike,
        'key_ce_levels': key_ce_levels,
        'key_pe_levels': key_pe_levels,
        'strength_indicator': strength_indicator
    }
    return render_template_string(template, **context)

if __name__ == '__main__':
    app.run(debug=True)
