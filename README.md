# Personal NIFTY Order Book & Option OI Screen (Upstox Analytics Token + Demo Mode)

Yeh aapka personal **NIFTY / BANKNIFTY Order Book + 1-Minute Candles + Option Open Interest** screen hai. Isme **Demo Mode** (bina broker account ke test karne ke liye) aur **Upstox Analytics Token Live Mode** dono built-in hain.

---

## 1. Render.com Par Online Kaise Karein + Upstox Token Kahan Rakhein?

Is folder mein **`render.yaml`** aur **`.python-version`** pehle se set hain taaki Render par 2 minute mein live ho jaye.

### Step A — Files Apne Private GitHub Repo Mein Upload Karein
1. [github.com/new](https://github.com/new) kholein, ek **Private** repository banayein (naam rakhein e.g. `my-nifty-orderbook`).
2. **"uploading an existing file"** par click karein aur `my-orderbook-render.zip` extract karke uski saari files (`app.py`, `index.html`, `requirements.txt`, `render.yaml`, `.python-version`, `.gitignore`, `README.md`) drag-and-drop karke **Commit changes** daba dein.
   *(Dhyan rahe: apni `.env` file GitHub par kabhi upload na karein!)*

### Step B — Render Mein Connect Karein (2 Tarike)

#### Tarika 1: 1-Click Blueprint (Sabse Aasan)
1. [dashboard.render.com](https://dashboard.render.com) par login karein → **New +** → **Blueprint** chunein.
2. Apna `my-nifty-orderbook` GitHub repo select karein (`render.yaml` khud detect ho jayega).
3. Render aapse do boxes poochhega:
   * **`UPSTOX_ANALYTICS_TOKEN`** → Yahan apna Upstox Analytics Token paste karein.
   * **`APP_PASSWORD`** → Koi apna personal password rakhein (taaki aapke online Render link ko aapke alawa koi aur na khol sake — broker rules ke liye zaroori!).
4. **Apply / Deploy** dabayein — 1–2 minute mein aapka `https://nifty-orderbook-screen-xxxx.onrender.com` live ho jayega!

#### Tarika 2: Manual Web Service
1. Render Dashboard → **New +** → **Web Service** → apna GitHub repo chunein.
2. Settings bharein:
   * **Runtime:** `Python 3`
   * **Build Command:** `pip install -r requirements.txt`
   * **Start Command:** `python app.py`
3. Neeche **Environment Variables** section mein **"Add Environment Variable"** click karke yeh add karein:
   * Key: `UPSTOX_ANALYTICS_TOKEN`  |  Value: *(Aapka Upstox Analytics Token — bina quotes/space ke)*
   * Key: `APP_PASSWORD`            |  Value: *(Koi apna secret password, e.g. `MyNifty@2026`)*
   * Key: `SOURCE`                  |  Value: `upstox`
   * Key: `PYTHON_VERSION`          |  Value: `3.11.9`
4. **Deploy Web Service** click karein! Jab browser mein apna Render URL kholein aur password maange, toh Username mein kuch bhi (`admin`) aur Password mein apna `APP_PASSWORD` daalein.

---

## 2. Apne Computer (Local PC) Par Upstox Token Kahan Rakhein?

1. Is folder mein **`.env`** file kholein (jo `.` dot se shuru hoti hai).
2. `UPSTOX_ANALYTICS_TOKEN=` ke bilkul baad apna token paste karein (bina kisi space ya quotes `"` ke):
   ```env
   UPSTOX_ANALYTICS_TOKEN=eyJ0eXAiOiJKV1QiLCJhbGciOi...
   ```
3. File save karein aur browser screen ke top-right bar mein **`Upstox Live`** (ya **`Retry Upstox`**) button dabayein. Server restart karne ki bhi zaroorat nahi hai!

> **Security Tip:** Apna token kabhi kisi chat, screenshot ya GitHub code mein mat daaliye. Local PC par `.env` mein rakhein, aur Render par sirf **Environment Variables** tab mein rakhein!

---

## 2. Roz Screen Kaise Start aur Stop Karein? (Start & Stop Each Day)

### Windows par:
* **One-Click Start:** Bas `start_windows.bat` par double-click karein — yeh khud packages check karega, server chalayega aur `http://127.0.0.1:8000` browser mein khol dega.
* **Manual Terminal se:**
  ```powershell
  cd my-orderbook
  python -m venv .venv
  .venv\Scripts\activate
  pip install -r requirements.txt
  set HOST=127.0.0.1
  python app.py
  ```
* **Stop karne ke liye:** Terminal window mein **`Ctrl + C`** dabayein.

### macOS / Linux par:
* **One-Click / Script Start:** `./start_mac_linux.sh` chalayein.
* **Stop karne ke liye:** Terminal mein **`Ctrl + C`** dabayein.

> **Market Timings:** Live NSE F&O order book **09:15 AM se 03:30 PM IST (Mon–Fri)** ke beech live chalti hai. Market band hone par bhi yeh last closing candles, quote aur OI dikhayega, aur **Demo Mode** 24×7 kabhi bhi chala sakte hain.

---

## 3. Jab Token Expire Ho Jaye Toh Kya Karein? (Token Renewal)

Normal broker tokens roz expire hote hain, lekin **Upstox Analytics Token ki validity 1 saal (1 Year)** hoti hai aur iske liye kisi Static IP ya OAuth login redirect ki zaroorat nahi padti!

Jab 1 saal baad (ya revoke karne par) token expire ho jaye (`HTTP 401`):
1. [Upstox Developer Apps → Analytics Tab](https://account.upstox.com/developer/apps#analytics) kholein.
2. **Generate Token** → **Confirm** par click karein aur copy icon se naya token copy karein.
3. Apni `.env` file mein `UPSTOX_ANALYTICS_TOKEN=` ke aage naya token paste karke save karein.
4. Web page par **Retry Upstox** click karein (ya `app.py` restart karein).

---

## 4. Har Setting Ka Table (Settings Reference)

| Setting | File | Default | Kya Badalta Hai (What It Changes) |
| :--- | :--- | :--- | :--- |
| `UPSTOX_ANALYTICS_TOKEN` | `.env` | *(empty)* | Aapka read-only Upstox Analytics Token |
| `SOURCE` | `app.py` / `.env` | `"demo"` | Startup mode (`"demo"` ya `"upstox"`; UI se bhi badal sakte hain) |
| `DEFAULT_SYMBOL` | `app.py` | `"NIFTY"` | Startup index future (`"NIFTY"` ya `"BANKNIFTY"`) |
| `BAND_SIZE` | `app.py` | `1.0` | Order book price grouping points (`1`, `2`, `5` pt UI se bhi badal sakte hain) |
| `DEMO_LEVELS` | `app.py` | `20` | Demo mode mein har side ke price levels |
| `DEMO_LOT_SIZE` | `app.py` | `65` | Demo mode ka lot size (Live mode Upstox se asli lot size khud padhta hai) |
| `HIGHLIGHT_TOP_N` | `app.py` | `3` | Dono sides par kitne sabse bade order bands solid highlight aur `#1..#3` label honge |
| `BOOK_REFRESH_SECONDS` | `app.py` | `1.0` | Demo order book kitne second mein update hogi |
| `OI_REFRESH_SECONDS` | `app.py` | `60` | Option Chain OI kitne second mein refresh hoga (API limit safe rakhne ke liye 60s) |
| `OI_RANGE_POINTS` | `app.py` | `500` | Price ke dono taraf kitne points tak ki strikes dikhengi (`±500` NIFTY, `±1500` BANKNIFTY) |
| `OI_STEP_POINTS` | `app.py` | `50` | NIFTY strikes ka gap (`50` pts) |
| `MAX_CANDLES` | `app.py` | `60` | Screen par kitni 1-minute candles rakhi jayengi |
| `RECONNECT_DELAY_SECONDS` | `app.py` | `10` | WebSocket drop hone par kitne second baad auto-reconnect hoga |
| `UPSTOX_DEPTH_MODE` | `app.py` / `.env` | `"full"` | `"full"` = Free 5 levels; `"full_d30"` = 30 levels (Upstox Plus Pack chahiye) |
| `HOST` / `PORT` | `app.py` / `.env` | `8000` | Server address aur port (`127.0.0.1` apne PC ke liye sabse safe hai) |
| `REFRESH_MS` | `index.html` | `1000` | Browser har kitne milliseconds mein screen redraw karega |
| `ALERT_LOTS_THRESHOLD` | `index.html` | `500` | Beep alert bajne ke liye ek band mein minimum lots (`Beep: On` hone par) |
| `ALERT_DISTANCE_POINTS` | `index.html` | `10` | Price ke kitne points ke andar bada order aane par beep bajegi |
| `--buy-solid` / `--sell-solid` | `index.html` | `#10b981` / `#ef4444` | Top 3 buy (green) aur sell (red) bands ke rang |

---

## 5. 5 Sabse Common Errors aur Unka Fix (Troubleshooting)

1. **"No Upstox Analytics Token found in .env" / "Token invalid or expired (HTTP 401)"**
   * **Karan:** `.env` mein token khali hai, quotes/spaces lag gaye hain, ya token revoke/expire ho gaya hai.
   * **Fix:** `.env` kholein, `UPSTOX_ANALYTICS_TOKEN=apna_token` bina space/quotes ke paste karein, save karein aur `Retry Upstox` dabayein.
2. **Page khulta hai par Order Book move nahi ho rahi**
   * **Karan:** Market band hai (NSE F&O sirf **09:15–15:30 IST, Mon–Fri** chalta hai).
   * **Fix:** Market hours mein check karein, ya testing ke liye upar **`Demo`** button dabayein.
3. **30-Level Mode (`full_d30`) mein depth nahi aa rahi**
   * **Karan:** Free Upstox account aur Analytics Token `"full"` mode mein **5 levels** dete hain; `"full_d30"` ke liye paid Upstox Plus Pack zaroori hota hai.
   * **Fix:** Banner mein **`Mode: 5-Lvl (Free)`** rakhein (ya `.env` mein `UPSTOX_DEPTH_MODE=full` rakhein).
4. **`HTTP 429` (Too Many Requests) ya WebSocket baar-baar disconnect hona**
   * **Karan:** Ek hi Upstox account par 2 alag jagah same program chal raha hai.
   * **Fix:** Dusri terminal/copy band karein aur 1 minute ruk kar `Retry Upstox` dabayein.
5. **`Python was not found` ya `ModuleNotFoundError: No module named 'upstox_client'`**
   * **Karan:** Python PATH mein nahi hai ya packages install nahi huye.
   * **Fix:** `pip install -r requirements.txt` chalayein (ya `start_windows.bat` use karein).
