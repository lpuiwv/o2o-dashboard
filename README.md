# O2O Shop DOP Dashboard（GitHub Pages 版）

每日 09:30（香港時間）自動由兩個 Google Sheet 攞數：

- **Daily Shop PNL (DOP bot)**：DM / AM summary、每間舖 DOP
- **All Shop Daily TM Cost**：每間舖 TM cost

攞到嘅數會用你設定嘅密碼加密，再更新網站。網站係公開網址，但係冇密碼嘅人只會見到登入頁，解唔開啲數。

---

## 一次性設定（大約 20 分鐘）

### 第 1 步：開 GitHub repo
1. 登入 <https://github.com> → 右上角 **+** → **New repository**
2. Repository name 打 `o2o-dashboard`，揀 **Public**（免費帳戶要 Public 先開到 Pages；repo 入面只有程式碼，冇數據）
3. 撳 **Create repository**
4. 喺新 repo 頁面撳 **uploading an existing file**，將 zip 解壓後**全部檔案同資料夾**拖入去（包括 `.github` 資料夾）→ **Commit changes**

> 如果 `.github` 資料夾拖唔入：撳 **Add file → Create new file**，檔名打 `.github/workflows/daily.yml`，再將 `daily.yml` 嘅內容貼入去。

### 第 2 步：開 Google service account（畀 GitHub 讀 Sheet 用）
1. 去 <https://console.cloud.google.com> → 開一個新 project（例如 `o2o-dashboard`）
2. 上面搜尋 **Google Sheets API** → **Enable**
3. 左邊 **IAM & Admin → Service Accounts → Create service account**，名打 `dashboard-reader` → **Done**
4. 撳入個 service account → **Keys → Add key → Create new key → JSON**，會 download 一個 `.json` 檔
5. 記低個 service account 嘅 email（類似 `dashboard-reader@xxx.iam.gserviceaccount.com`）

> 請先確認公司政策容許將呢兩個 Sheet share 畀外部 service account。

### 第 3 步：將兩個 Google Sheet share 畀 service account
喺兩個 Sheet 撳 **Share**，貼上 service account email，權限揀 **Viewer**（唔好揀 Editor），唔使通知。

### 第 4 步：喺 GitHub 加 secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**，加兩個：

| Name | Value |
|---|---|
| `GOOGLE_SA_JSON` | 第 2 步 download 嗰個 `.json` 檔嘅**全部內容**（用記事本打開再 copy） |
| `DASH_PASSWORD` | 你想同事用嚟開網站嘅密碼（最少 10 個字，越長越好） |

加完之後，喺電腦刪咗個 `.json` 檔。

### 第 5 步：開 GitHub Pages
Repo → **Settings → Pages** → **Source** 揀 **GitHub Actions**

### 第 6 步：第一次執行
Repo → **Actions** → 左邊 **Daily dashboard refresh** → **Run workflow**。等 1 至 2 分鐘變綠剔，網址會喺 **Settings → Pages** 顯示（類似 `https://你的帳戶.github.io/o2o-dashboard/`）。

將網址同密碼分開畀同事，例如網址用 email，密碼用電話講。

---

## 日常
- **自動更新**：每日 09:30 自動跑，唔使理。
- **即時更新**：Actions → Run workflow。
- **換月**：程式會自動搵當月 tab（例如 `Summary - Nov 2026`、`TM Cost by Shop 202611`）。如果新月份 tab 未開，就會繼續用上個月。**Tab 名格式唔好改**。
- **改密碼**：改 `DASH_PASSWORD` secret，再 Run workflow。舊密碼即時失效。
- **注意**：GitHub 會喺 public repo 冇任何更新 60 日之後暫停排程。收到 email 通知嘅話，入 Actions 撳 **Enable workflow** 就得。

## 出錯點算
Actions 入面紅色 ✗ → 撳入去睇 log：
- `403` / `permission`：第 3 步未 share 或者 email 打錯
- `Unable to parse range`：Sheet tab 名改咗
- `DASH_PASSWORD ... shorter than 10`：密碼太短

## 檔案
- `index.html`：網站（登入頁 + dashboard，喺 browser 入面解密）
- `d3.min.js`：圖表 library（d3 v7.9.0）
- `fetch_data.py`：讀 Sheet → 加密 → 輸出 `site/`
- `.github/workflows/daily.yml`：每日排程
