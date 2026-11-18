# 途迹（Hoptrace）

轨迹点装载 → 运单时间线物化 → 迟到旁路与脏点隔离 → 状态证明互证。

**不是** Feed 归并、CDC 游标 Sink、质检签发、数仓分层、交班过户、正冲净额。  
**也不是** 电商订单 / 仓库库存 / 外卖工单 CRUD——去皮后是事件装载与有序物化。

## 启动

```bash
# 后端
cd backend
pip install -r requirements.txt
set DATA_DIR=%CD%\data
set FIXTURES_DIR=%CD%\..\fixtures
uvicorn app.main:app --host 127.0.0.1 --port 10800

# 前端
cd frontend
npm install
npm run dev
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://127.0.0.1:5800 |
| 接口 | http://127.0.0.1:10800 |

## UI

三栏测绘板：顶品牌条 + 中竖轴时间线 + 右检查器 + 底 hop 点阵。  
不使用左轨编号导航、不使用顶栏流程 ribbon。

## 幂等装载

- 业务键：`waybill_id | event_ts | location_code | event_code`（四段 `|` 连接）。
- 落库形态：独立装载回执表 `ingest_receipts`，业务键为主键，记录成功落账的正式点 id；
  幂等判断只查库，不用进程内 Set。
- 路径：装载前按业务键查回执 → 命中则整键跳过并写 `skip` 证明（不插点、不改投影）；
  未命中但正式线已有该键的点，则回填回执后跳过；都没有才插入正式点 + 回执 + `write` 证明。
- 失败闭：回执存在但正式点缺失 / 已撤回 / id 或键字段不符，装载整体回滚并报错。
- 证明：`ingest_proofs` 记录批次、业务键、动作（`write` / `skip`）、正式点 id、原因；
  `ingest_runs` 记录每轮写入 / 跳过与当轮正式点总数。

接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/ingest/run` | 跑一批装载（不传 body 读本地夹具）；重复跑同夹具第二遍应全部跳过 |
| GET | `/api/ingest/proofs?batch_id=...` | 按批次查幂等证明，附轮次汇总；可加 `run_id` / `action` |
| GET | `/api/ingest/receipts?business_key=...` | 查装载回执 |
| GET | `/api/ingest/runs?batch_id=...` | 查每轮装载汇总 |

测试：`cd backend && python -m pytest tests -q`

