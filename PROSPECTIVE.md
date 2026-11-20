# 揭盲前预报评分

`README.md` 记录现有事后复盘。前瞻评分使用同一批次清册和 `bench.reveal.reveal_result` 的实际首揭。仅私存实测时仍可锁定，首次揭盲提交后该片的资格即固定。前瞻流程继续使用独立的本地库连接及可分批送达的来件。

## 试验端

- `bench.reveal.lock_forecast(studies, bench, batch_id, specimen_id, request_id, probability, source, *, custodian=None)` 提交具名预报。概率、名称、批片身份沿用 V1。调用方可传入独立的 `AccessionStore` 作为 `custodian`，以便接线时把正文保管安排在评估侧。试验端自行保管时，可省略该参数。实际揭盲始终使用原入口，不依赖预报源或评估库。
- 正式裁决以试验端提交为准。返回字典含 `batch_id`、`specimen_id`、`request_id` 和 `status`。成功为 `locked`。首次请求发生在本片首揭后为 `revealed`，已有其他请求锁定且本片尚未揭盲时为 `already_locked`。这两个拒绝裁决也按请求号重领。请求号在批内唯一。已有裁决时，同片、等值有理概率、同出处构成同参，原样返回首裁决。同号改片、改概率或改出处报 `ValueError`。锁定先于揭盲且生效之时，原概率、原出处和身份必须已经由预报源以外的一方持久保有。仅发出消息、只存引用或仍待收到的正文均不足以锁定。
- `bench.reveal.read_locked_forecast(bench, batch_id, request_id, *, custodian=None)` 领出成功请求的原概率件。源库关闭后仍能领取。同一锁定签出的件保持一致。请求号有效但未知时报 `KeyError`，已知拒绝裁决报 `ValueError`。
- `bench.closeout.close_prospective(studies, bench, batch_id)` 交付前瞻封批件。全清册实际揭盲才可封批，未齐报 `ValueError`。固定结果、实测出处及每片锁定或未锁定的资格，按同批重复调用领回原件。既有 `close_results` 仍可用于事后复盘。

## 交接与评估

两类件都是可独立送达的 JSON 对象。沿用 V1 的 `batch_id`、`roster_id`、`specimens`、`kind` 和 `source_id` 信封及有理数编码。

- 概率件 `kind` 为 `prospective_forecast`，`source_id` 为锁定请求号。正文含 `specimen_id`、`probability`、`source`、`binding`。
- 封批件 `kind` 为 `prospective_results`，`source_id` 为固定封批号。`rows` 覆盖全清册，各行含 `specimen_id`、整数 `outcome`、实测 `source` 和 `lock`。未锁定的 `lock` 为 `null`，已锁定的 `lock` 含 `request_id` 和 `binding`。它与实测同属固定封批，概率正文另行交接。
- `binding` 是供该批复核的不透明非空字符串。评估必须能核对它所对应的确切批次、清册、试片、请求号、有理概率和预报出处。保留请求号但改变正文、交换试片或用另一版替换，均不能兑现原锁定。来件来自项目的业务交接入口，核对内容及身份即可。
- `evaluation.accession.accept_piece(accessions, payload)` 接纳两类新件，返回接纳号。等价重投返首号，同类型同批同来源号异正文报 `ValueError`。两类件可先后任意送达，同一个评估库中的接纳件可以跨调用保留。
- `evaluation.review.issue_prospective_review(accessions, archive, results_accession, forecast_accessions)` 仅消费指定封批与本次送审的概率件，`forecast_accessions` 为可空的非重复编号列表或元组。揭盲前已锁定与正文已送审是两项事实。送审材料的类型、身份或绑定失配报 `ValueError`，未知有效编号报 `KeyError`。坏件可在接纳时拒绝。核对给到的全部材料后，若某个已锁定片仍缺确切正文，返回 `{"status": "pending", "missing": [...]}`，缺片按清册序排列，归档无新增。不得缩小分母或改查未送审件。无锁定的片不进入分母。
- 齐备时返回 `{"status": "issued", "report_id": "…", "receipt": {"report_id": "…"}, "body": {...}}`。正文固定 `batch_id`、`roster_id`、`results_accession`、`results_closeout`，以及 `included`、`excluded` 和 `mean`。两组均按清册序排列。两组行都给出 `specimen_id`、`outcome` 和 `result_source`，纳入行另给出 `request_id`、`forecast_accession`、`forecast_source`、`probability` 和 `squared_error`。均值为纳入片 `(p−y)²` 的等权精确平均。报告字典使用 V1 JSON 有理编码。无锁定仍可签发，`included` 为空且 `mean` 为 `None`。有纳入片且零误差时，`mean` 为 `{"numerator": 0, "denominator": 1}`。
- `archive.lookup.read_prospective_review(archive, report_id)` 按号领回同一份已签字典。签发原子保存正文和回执，真实存储失败传播且两者均无新增，可按原件重试。后续勘误、接纳其他件或重开档案均不影响已签值。非法输入或矛盾证据报 `ValueError`，未知有效批片号或正式成果号报 `KeyError`。V1 来件、接口、编码及已归档报告继续支持原复盘。

## 样例与运行

`examples/prospective_cases.json` 给出清册 C、B、A、实测、三份候选概率和 A 的迟到更正。先锁 A、C，在 A 首次裁决已提交后丢弃应答，再实际揭盲。B 先揭盲再请求。关闭预报源并重取 A 原裁决。送达封批和 C 正文时只应列 A 待证，也可以先接纳 C 正文再送封批。送达 A 原件后，纳入 C、A，排除 B，A 分项 9/16，均值 9/32。用 A 的迟到版本或保留原身份但更改的概率正文替代，不能签出报告。只锁 A 的两片对照均值为 9/16。无锁定，以及只锁定预测为 1 且实测为 1 的片，分别得到 `None` 和精确零。

基线中 `python3 -B -m examples.forecast_order` 会跑两次真实 V1 复盘。两次交换首份概率的冻结和接纳与首揭的先后，来件及归档仍完全一致。它演示事后复盘所能提供的证据。

沿用 `.pointwise-runtime.json` 的版本 1 和 `cli` 入口。`setup` 为命令参数数组的列表，`verify` 和 `demo` 各为一条命令的参数数组。交付时 `verify` 运行原回归和新增前瞻回归，业务断言失败返回非零。`demo` 演示丢回包后重领、断源待证、齐证签发和档案重开后领取实际结果。原事后样例仍可直接运行。
