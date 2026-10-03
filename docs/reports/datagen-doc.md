# 使用者參數說明 — Harmful Prompt 生成

所有參數集中在單一 YAML config。相對路徑以 **config 檔所在目錄** 為基準，
不是執行時的工作目錄，因此從任何目錄執行結果都相同。

```bash
export NCHC_API_KEY=...
export PYTHONPATH=dags/scripts
python3 -m harmful_prompt.generate run --config configs/example.yaml
```

---

## 生成內容

| 參數 | 型別 | 預設 | 說明 |
|---|---|---|---|
| `policy_file` | path | **必填** | 政策 JSONL 檔路徑。 |
| `domain` | str | **必填** | 服務情境，例如 `TAIWAN AI RAP 客服`。會寫入每筆輸出的 `domain` 欄位。 |
| `total` | int | `30` | 總生成筆數，平均分配給各政策。 |
| `policy_ids` | list | `[]` | 只生成指定政策；留空代表檔案中全部。 |
| `language` | str | `Traditional Chinese (Taiwan)` | 生成語言。 |
| `severity_ratio` | dict | `low .3 / medium .4 / high .3` | 各嚴重度比例，會自動正規化，可直接填百分比。 |

**分配為精確分配。** 採最大餘數法，因此各項加總必定等於 `total`，不會因四捨五入短少或多出。

---

## 輔助文件（選填）

| 參數 | 型別 | 預設 | 說明 |
|---|---|---|---|
| `aux_documents` | list | `[]` | 補充領域知識的文件路徑。**僅支援 `.txt` / `.md` / `.jsonl`**，其他格式會直接報錯而非默默略過。 |
| `aux_char_budget` | int | `6000` | 所有輔助文件合計注入 prompt 的字元預算（字元數，非 token 數）。 |

字元預算會平均分配給各份文件（`預算 ÷ 文件數`），因此單一長文件不會把其他文件擠掉；
超出的部分會截斷並標記 `…(truncated)`。

**注意：每份文件有 200 字元的下限。** 若文件數過多，`預算 ÷ 文件數` 會低於 200，此時以
200 為準，實際注入量會超過設定的預算 —— 有效上限其實是
`max(aux_char_budget, 200 × 文件數)`。設下限是因為每份只給 120 字元並無意義，寧可超出預算
也不要注入無用的片段。以預設 6000 計算，文件數超過 30 份時就會發生。

`.jsonl` 會依序嘗試 `text` / `content` / `body` / `prompt` / `description` 欄位取出文字；
若都沒有，則保留整筆記錄，確保特殊 schema 的領域詞彙仍能進入 prompt。

---

## 模型

| 參數 | 型別 | 預設 | 說明 |
|---|---|---|---|
| `model` | str | `Mistral-Large-3-675B-Instruct-2512` | 生成模型。此為 27 模型 smoke test 的第一名。 |
| `base_url` | str | `https://your-openai-compatible-endpoint/v1` | OpenAI 相容 API 端點。 |
| `api_key_env` | str | `NCHC_API_KEY` | 存放 API key 的**環境變數名稱**。config 只記錄變數名，永遠不記錄 key 本身。 |
| `temperature` | float | `1.0` | 較高以取得情境多樣性。 |
| `max_tokens` | int | `4096` | 單次回應長度上限。 |
| `max_prompts_per_call` | int | `5` | 單次呼叫最多要求幾筆。調低可降低回應被截斷的風險。 |
| `concurrency` | int | `4` | 同時進行的 API 呼叫數。 |
| `max_retries` | int | `3` | 單次呼叫最多重試次數。 |
| `timeout` | float | `300` | 單次呼叫逾時秒數。 |

---

## 輸出

| 參數 | 型別 | 預設 | 說明 |
|---|---|---|---|
| `output_dir` | path | `output` | 輸出根目錄，實際結果寫入 `<output_dir>/<run_id>/`。 |
| `run_id` | str | `null` | 留空則使用 UTC 時間戳記。 |

### 產生的檔案

| 檔案 | 內容 |
|---|---|
| `harmful_prompts.jsonl` | 資料集本體，每行一筆。 |
| `run_manifest.json` | 執行參數、輸入檔 SHA-256、產出統計、各解析策略次數。 |
| `failures.jsonl` | 失敗紀錄（僅在有失敗時產生），含失敗階段與原因。 |
| `dry_run_prompts.jsonl` | `--dry_run` 時產生，內含實際會送出的完整 prompt。 |

### 輸出欄位

| 欄位 | 說明 |
|---|---|
| `id` | 由「政策 + prompt 內容」雜湊而得，同一筆內容跨執行皆相同，便於跨批次去重。 |
| `prompt` | Harmful Prompt 本體。 |
| `violated_policy` | 違反的政策 `policy_id`。 |
| `severity` | `low` / `medium` / `high`。 |
| `domain` | 服務情境。 |
| `generation_rationale` | 模型說明違反哪一條、為何屬該嚴重度。 |
| `meta` | provenance：模型、解析策略、政策原文、run_id。 |

---

## CLI 覆寫

無需修改 config 即可覆寫少數常用參數：

```bash
python3 -m harmful_prompt.generate run --config configs/example.yaml \
    --total 180 --model "Gemma-3-TAIDE-12b-Chat" --output_dir ./dataset
```

`--dry_run` 會完整建立計畫並渲染所有 prompt 寫入檔案，但**不呼叫 API、不需要 API key**，
適合在新增政策檔後先行檢查。

---

## 嚴重度詞彙對照

正式值為 `low` / `medium` / `high`。輸入可使用下列任一種寫法，系統會自動正規化：

| 輸入 | 正規化為 |
|---|---|
| `low`、`minor`、`Level 1`、`L1`、`1` | `low` |
| `medium`、`moderate`、`mid`、`Level 2`、`L2`、`2` | `medium` |
| `high`、`severe`、`critical`、`Level 3`、`L3`、`3` | `high` |

因此既有的 TAIWAN AI RAP 政策檔（使用 minor/moderate/severe）無需改寫即可直接使用。

若政策只定義部分層級，未定義者會採用內建的通用定義，仍保證三個層級齊全。


---

## 品質階段參數

三個階段預設全部關閉，既有的生成流程不受影響。

- `generators`：選填的加權生成模型清單；留空時仍以 `model` 作為唯一生成模型。
  權重會正規化後，在**每一個 policy × severity 分組內**精確分配，而不是只讓全體
  總數湊出比例。
- `judge`：選填的裁判階段設定。`base_url` 與 `api_key_env` 留 null 時，會沿用根層級的設定。
- `semantic_dedup`：選填的本地 sentence-transformers 語意去重設定。

---

## 完整流程

四個階段各自獨立，都讀檔、寫檔，可以分開重跑。

```bash
# 1. 乾跑：完整建立計畫並渲染 prompt，但不呼叫 API、不需要 API key
python3 -m harmful_prompt.generate run --config configs/example.yaml --dry_run

# 2. 生成
python3 -m harmful_prompt.generate run --config configs/example.yaml

# 3. 裁判（需在 config 中設定 judge.enabled: true）
python3 -m harmful_prompt.judge run --config configs/example.yaml \
    --input output/example-run/harmful_prompts.jsonl

# 4. 語意去重（需要選配套件，見下方）
python3 -m harmful_prompt.semantic_dedup run --config configs/example.yaml \
    --input output/example-run/accepted_prompts.jsonl

# 5. 以人工標註評估裁判品質
python3 -m harmful_prompt.evaluate run \
    --gold datasets/judge_gold.jsonl \
    --predictions output/example-run/judged_prompts.jsonl
```

### 裁判階段的產出

| 檔案 | 內容 |
|---|---|
| `judged_prompts.jsonl` | **每一筆**來源資料，附上裁判結果或 `judge_error` |
| `accepted_prompts.jsonl` | 通過的資料：政策相符、不需外部資訊，且（若有設定）嚴重度相符 |
| `judge_rejected.jsonl` | 裁判明確判定為 `non_violation` 的資料 |
| `judge_review.jsonl` | 需人工檢視：模稜兩可、需要外部資訊、metadata 不一致、API 失敗、無法解析 |
| `judge_manifest.json` | 各類計數、模型設定、政策與樣板雜湊、檔案雜湊 |

**沒有資料會被丟掉。** 每一筆來源資料一定會出現在 accepted / rejected / review 其中
**恰好一個**檔案裡。API 失敗與無法解析的回覆一律進 review，不會被靜默捨棄——裁判無法
判斷的資料，仍然需要人看過。

### 裁判是盲判的

裁判只看到「編譯後的政策」與「prompt 文字」。生成階段寫入的
`generation_rationale`、宣稱的 `severity`、以及 `generator_model` **都不會**送進裁判的
prompt。這是刻意的：一旦告訴裁判「生成模型認為這是 medium 違規」，判斷就不再獨立，
accepted 資料集也就變成生成模型自己幫自己打分數。

裁判獨立作答之後，才會把它的結論和該筆資料原本宣稱的內容做比對，比對結果記在
`policy_match` / `severity_match` / `accepted` 三個欄位。

### 語意去重

需要額外安裝選配套件（基礎安裝不含）：

```bash
pip install -r requirements-semantic.txt
```

`threshold` 是餘弦相似度門檻，預設 `0.90`。**請用自己的資料校準**：先在一小批已知的
重複／非重複資料上試跑，看 `semantic_duplicates.jsonl` collapse 掉的是不是真的重複，
再決定要放寬還是收緊。門檻沒有通用的正確值。

宣稱**不同政策**的資料永遠不會被合併。同一句 prompt 同時違反兩條規則，那是兩筆資料；
跨政策的高相似度只會記在 `dedup_manifest.json` 的 `cross_policy_audit` 供人稽核。

### 評估裁判品質

`evaluate` 階段是純算術，不呼叫任何模型。它用 `id` 把人工標註檔和
`judged_prompts.jsonl` 對起來。

人工標註檔每行需要 `id`、`policy_id`、`gold_verdict`、`gold_severity` 四個欄位。

兩件事要注意：

1. **沒有預測的資料算「未涵蓋」，不算成隱含的 non_violation。** 把裁判失敗的資料當成
   正確拒絕，會正好在這個階段最該揭露的地方灌水。因此 `coverage` 要和
   precision / recall 一起看。
2. **held-out policy 才算數。** 政策條件式裁判的賣點是「換一條沒看過的政策也能用」，
   所以評估必須用開發過程中**沒有**用來調整 prompt 或門檻的政策，否則量到的是擬合程度，
   不是泛化能力。

`samples/` 底下的檔案是**模型生成的示例，不是人工標註**，不可以拿來當 gold。
