# Demo 操作手冊 — 逐步自己跑一遍

給你自己照著做一遍用的。每一步都寫清楚：**在哪個目錄、下什麼指令、該看到什麼、
哪個檔案負責這件事**。跑完你就能對 mentor 完整講一次流程。

**前八步完全不需要 API key，也不會產生任何費用**（`--dry_run`）。只有第 6 步會真的呼叫 API。

---

## 0. 環境準備

```bash
cd ~/airflow-datagen-harmful-prompt
export PYTHONPATH=dags/scripts
```

`PYTHONPATH` 一定要設，否則 `import harmful_prompt` 會失敗。這個路徑刻意與 Airflow
worker 的 `dags/scripts` 一致，之後搬上去不用改。

確認可以跑：

```bash
python3 -m harmful_prompt.generate run --help
```

---

## 1. 檔案地圖（先知道東西在哪）

| 你要看什麼 | 檔案 |
|---|---|
| **設定檔（demo 主角）** | `configs/example.yaml` |
| 政策定義 | `configs/policies/example_platform.jsonl` |
| 另一個領域的政策（通用性用） | `configs/policies/demo_bank.jsonl` |
| 輔助文件 | `configs/aux/service_overview.md` |
| **入口程式** | `dags/scripts/harmful_prompt/generate.py` |
| 政策讀取／severity 正規化 | `dags/scripts/harmful_prompt/policy.py` |
| 數量與比例分配 | `dags/scripts/harmful_prompt/planner.py` |
| 組 prompt | `dags/scripts/harmful_prompt/prompt_builder.py` |
| 呼叫 API | `dags/scripts/harmful_prompt/client.py` |
| 解析模型回應（含截斷救援） | `dags/scripts/harmful_prompt/parser.py` |
| 輸出欄位定義 | `dags/scripts/harmful_prompt/schema.py` |
| 設定檔驗證 | `dags/scripts/harmful_prompt/config.py` |
| 已產出的資料集範例 | `samples/` |
| 參數說明文件 | `docs/reports/datagen-doc.md` |
| 報告 | `docs/reports/harmful_prompt_pipeline_20260831.md`（+ `.zh-TW.md`） |

---

## 2. 先看輸入：政策長什麼樣

```bash
python3 -c "
import json
for l in open('configs/policies/example_platform.jsonl'):
    r = json.loads(l)
    print(r['policy_id'], '| severity keys =', list(r['severity']), '| examples =', len(r['example_prompts']))
"
```

**應該看到：**

```
A1 | severity keys = ['minor', 'moderate', 'severe'] | examples = 3
A2 | severity keys = ['minor', 'moderate', 'severe'] | examples = 3
A3 | severity keys = ['minor', 'moderate', 'severe'] | examples = 3
```

**重點：** 檔案裡寫的是 `minor/moderate/severe`（舊命名），但系統正式值是
`low/medium/high`。`policy.py` 的 `SEVERITY_ALIASES` 自動對應，所以**舊的政策檔不用改**。

---

## 3. 第一次 dry run：看「計畫」

```bash
python3 -m harmful_prompt.generate run --config configs/example.yaml --dry_run
```

**應該看到：**

```
policy         low  medium  high   total
A1               9      12     9      30
A2               9      12     9      30
A3               9      12     9      30
TOTAL                                 90
```

`configs/example.yaml` 裡寫 `total: 90`、`severity_ratio: 0.3/0.4/0.3`，
90 × 0.3 = 27，分到三個政策各 9 —— 表格就是這樣算出來的。

**重點：** 分配用最大餘數法，**加總一定等於 90**，不會因四捨五入短少。

---

## 4. 改 YAML，看數字跟著動 

打開 `configs/example.yaml`，改 `total: 90` 成 `total: 30`，再跑一次同一行指令：

```bash
python3 -m harmful_prompt.generate run --config configs/example.yaml --dry_run
```

**應該看到全部變成 1/3：**

```
A1               3       4     3      10
A2               3       4     3      10
A3               3       4     3      10
TOTAL                                 30
```

再改 `severity_ratio` 成：

```yaml
severity_ratio:
  low: 0
  medium: 0
  high: 1
```

**應該看到全部集中在 high：**

```
A1               0       0    30      30
```

再改 `policy_ids: []` 成 `policy_ids: ["A1"]` → 只剩 A1 一行。

**改完記得把 `configs/example.yaml` 改回原值**（`total: 90`、比例 0.3/0.4/0.3、
`policy_ids: []`），或直接：

```bash
git checkout configs/example.yaml
```

> 想一次看完所有變化而不用手改，跑這個：
> ```bash
> bash demo/demo_yaml_control.sh
> ```

---

## 5. 看實際送出去的 prompt 長什麼樣

`--dry_run` 會把完整 prompt 寫成檔案：

```bash
python3 -m harmful_prompt.generate run --config configs/example.yaml --dry_run
ls -t output/*/dry_run_prompts.jsonl | head -1
```

看 system message：

```bash
python3 -c "
import json, glob
f = sorted(glob.glob('output/*/dry_run_prompts.jsonl'))[-1]
print(json.loads(open(f).readline())['system'])
"
```

看某一次呼叫的 user message：

```bash
python3 -c "
import json, glob
f = sorted(glob.glob('output/*/dry_run_prompts.jsonl'))[-1]
row = json.loads(open(f).read().splitlines()[1])
print(f\"policy={row['policy_id']} severity={row['severity']} count={row['count']}\n\")
print(row['user'])
"
```

**重點：** 每次呼叫**只注入一條政策 + 一個嚴重度**。
原型報告記載過，多類別混在同一次呼叫會造成跨類別漂移與 severity 標籤漂移。

### 證明輔助文件真的有進到 prompt

```bash
python3 -c "
import json, glob
f = sorted(glob.glob('output/*/dry_run_prompts.jsonl'))[-1]
t = open(f).read()
for w in ('Safety Guard Proxy', 'GOV113074', 'Mail-Relay'):
    print(f'{w:22s} 出現 {t.count(w)} 次')
"
```

這些詞**只存在於 `configs/aux/service_overview.md`**，政策原文裡沒有。
把 `aux_documents` 清空再跑一次，次數會變 0。

---

## 6. 真的生成（唯一需要 API key 的一步）

```bash
export NCHC_API_KEY=<你的 key>
python3 -m harmful_prompt.generate run \
    --config configs/example.yaml --total 6 --output_dir ./demo_run
```

**應該看到：**

```
A1:low x3 -> ok
A2:medium x3 -> ok
...
produced 6/6 prompts (0 failure(s)) -> .../harmful_prompts.jsonl
```

看結果：

```bash
python3 -c "
import json, glob, sys
files = sorted(glob.glob('demo_run/*/harmful_prompts.jsonl'))
if not files:
    sys.exit('尚未產生 demo_run/，請先跑上面那一步（需要 NCHC_API_KEY）')
for l in open(files[-1]):
    r = json.loads(l)
    print(f\"[{r['severity']:6s}] {r['violated_policy']}  {r['prompt'][:60]}…\")
    print(f\"         理由: {r['generation_rationale'][:60]}…\")
"
```

---

## 7. 看輸出與可追溯性

每次執行的目錄下有三個檔案：

| 檔案 | 內容 |
|---|---|
| `harmful_prompts.jsonl` | 資料集本體 |
| `run_manifest.json` | 參數、輸入檔 SHA-256、統計、解析策略次數 |
| `failures.jsonl` | 失敗紀錄（有失敗才產生） |

```bash
cat samples/taiwan_ai_rap_run_manifest.json | python3 -m json.tool | head -30
```

**show：**

```
"parse_strategies": {"json_salvaged": 7, "json_fence": 32}
"produced": 180, "requested": 180, "failures": 0
```

`json_salvaged: 7` = 39 次呼叫裡有 7 次回應被截斷、靠救援解析器救回來的。
沒有這個機制，那 7 次會整批作廢（約 19% 缺口）。

---

## 8. 錯誤處理示範

**故意打錯參數名：** 把 `configs/example.yaml` 的 `severity_ratio` 改成 `severity_ratios`：

```bash
python3 -m harmful_prompt.generate run --config configs/example.yaml --dry_run
```

**應該看到：**

```
harmful_prompt.config.ConfigError: unknown config key(s): ['severity_ratios'].
Known keys: ['api_key_env', 'aux_char_budget', ...]
```

**重點：** 打錯字**不會**被默默忽略然後套用預設值 —— 那種錯誤要等到有人稽核輸出分布
才會發現。這裡直接擋下並列出所有合法 key。

其他可以現場示範的錯誤：

| 你改什麼 | 會看到的錯誤 |
|---|---|
| `total: 0` | `ConfigError: total must be a positive integer, got 0` |
| `temperature: 5` | `ConfigError: temperature must be between 0 and 2, got 5` |
| `severity_ratio` 全設 0 | `PlanError: severity ratio must contain at least one positive weight` |
| `severity_ratio` 有負數 | `PlanError: severity ratio for 'low' must not be negative` |
| `policy_ids: ["Z9"]` | `PolicyError: policy_id(s) not in file: ['Z9']; available: ['A1','A2','A3']` |
| `policy_file` 指到不存在的檔 | `PolicyError: policy file not found: ...` |
| `aux_documents` 放 `.pdf` | `AuxDocError: unsupported auxiliary document format '.pdf'; supported: .txt, .md, .jsonl` |

一次跑完全部錯誤示範：

```bash
bash demo/demo_yaml_control.sh     # 第 8 節就是錯誤示範
```

**記得改完還原：** `git checkout configs/example.yaml`

---

## 9. 通用性：換一個領域，程式碼完全不動

```bash
python3 -m harmful_prompt.generate run --config configs/demo_bank.yaml --dry_run
```

**應該看到：**

```
policy         low  medium  high   total
B-FRAUD          2       2     2       6
B-LAUNDER        2       2     2       6
TOTAL                                 12
```

`configs/policies/demo_bank.jsonl` 只有兩行、**每行只有 `policy_id` 和 `policy`**，
沒有 severity 定義、沒有範例、沒有輔助文件，語言是英文，領域是銀行客服。

實際跑出來的結果在 `samples/demo_bank_harmful_prompts.jsonl`：

```bash
python3 -c "
import json
for l in open('samples/demo_bank_harmful_prompts.jsonl'):
    r = json.loads(l)
    if r['severity'] in ('low','high'):
        print(f\"[{r['severity']:5s}] {r['prompt'][:88]}…\")
" | head -4
```

low 是「父親過世想查他的帳戶」這種同情包裝的試探，
high 是「我是台北地檢署林警官」的完整冒充執法人員 —— **完全用內建預設 severity 量表**
分出來的，證明流程沒有任何 RAP 專屬邏輯。

---

## 10. 測試

```bash
python3 -m pytest -q tests/
```

**應該看到 `191 passed`。不需要網路、不需要 API key** —— end-to-end 測試用 stub 取代
API client，所以 CI 上也能跑。

---

## 給 mentor 的 10 分鐘 demo 建議順序

| 分鐘 | 做什麼 | 指令 |
|---|---|---|
| 0–1 | 講流程圖：政策 → 計畫 → prompt → LLM → 資料集 | 開 `README.md` |
| 1–3 | 看政策檔，說明只有 `policy_id` / `policy` 必填 | 第 2 步 |
| 3–5 | **改 YAML，數字跟著動** | 第 4 步 |
| 5–6 | 看實際送出的 prompt + 證明輔助文件有進去 | 第 5 步 |
| 6–7 | 真的生成 6 筆 | 第 6 步 |
| 7–8 | 看 manifest，指出 `json_salvaged: 7` 的意義 | 第 7 步 |
| 8–9 | 打錯參數名 → 立刻報錯 | 第 8 步 |
| 9–10 | 換銀行政策，證明不綁領域 | 第 9 步 |

想全自動跑完，兩個腳本：

```bash
bash demo/demo_workflow.sh        # 完整流程，八個階段
bash demo/demo_yaml_control.sh    # 專講 YAML 控制參數
```

`demo_workflow.sh` 若有設 `NCHC_API_KEY` 會做一次真的呼叫，沒設就自動跳過那一階段。
