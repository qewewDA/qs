# 供 AI 理解流派的全局机制与数值资料库

知识范围包括全局机制。直接控制的当前开发重点是星球建设与飞船操作，内阁、传统和飞升由用户选择；AI 可以读取这些选择、查询搭配与前置，并据此调整建设建议。

## 数据来源与覆盖

资料从本机 **Cygnus v4.5.2 (9776)** 的安装数据编译，保留中文/英文名称、描述、定义文件及 SHA-256。当前导出 22,539 条定义，覆盖 92 类目录，包括：

| 范围 | 已编译内容 |
| --- | --- |
| 帝国流派 | 49 个飞升天赋、234 个传统、33 个传统组、77 个起源、281 个国民理念、政体/权力制度/思潮 |
| 内阁与人口 | 内阁职位/议程、领袖类别/特质、物种特质/权利、岗位与人口类别 |
| 星球经济 | 建筑、区划、分区与槽位、资源、殖民地类型、星球修正、决议、法令、政策、地块 |
| 太空与军事 | 舰船尺寸、战斗行为、组件、区段、恒星基地、军队、轰炸、巨构 |
| 进程机制 | 科技、遗珍、考古、异常、特殊项目、星界、局势、危机 |
| 帝国关系 | 外交行动、战争目标、联邦、星海共同体决议、附属协议、间谍、隐形 |
| 规则依据 | 静态修正、脚本触发器/效果/数值、game_rules、2,144 个 defines 常量 |

计数包含带条件或歧义的条目，不代表全部可用。当前有 44 条重复 ID 标为 ambiguous。DLC、科技、起源、国民理念等条件仍需结合当前帝国判断。此资料也不能替代尚未开放的引擎硬编码规则。

官方英文 Wiki 与用户提供的灰机 Wiki 均已尝试页面、API 和公开读取入口。当前环境得到挑战页面或 401/403，**正文导入数为 0**。`wiki_status` 显式记录此限制。可在取得页面导出后补入标题、原 URL、修订、许可、适用版本与正文；挑战 HTML 不能作为知识正文导入。

## MCP 如何查询

```json
{"tool":"search_game_knowledge","arguments":{"query":"研究实验室","category":"buildings"}}
{"tool":"get_game_definition","arguments":{"category":"buildings","object_id":"building_research_lab_1","sections":["base_buildtime","resources","prerequisites"]}}
{"tool":"get_game_definition","arguments":{"category":"ascension_perks","object_id":"ap_mind_over_matter"}}
{"tool":"get_game_definition","arguments":{"category":"defines","object_id":"NPop.POP_BULK_UNIT_SIZE"}}
```

`knowledge_status` 返回实际版本、各类别数量、警告和 Wiki 正文数量。`get_wiki_reference` 只返回已经导入的明确页面。未找到不代表机制不存在。

`request_observation` 的 `empire_build` 可读取实际政府/国民理念/起源、思潮和已选传统/飞升等上下文，部分存档字段缺失时保持 unknown。AI 需要把这些选择与对应条目、当前资源、星球及舰船状态一起看。

## 数值使用原则

1. **基础成本与实际成本分别记录。** 例如研究实验室定义的普通帝国分支为 400 矿物、基础工期 360、建筑维护费 2 能量；游牧分支、减耗/建造速度等修正另行适用。
2. **条件分支保留。** 重复 `cost` 或 `modifier` 不能无条件相加。符号变量与 inline_script 会展开；未解析值显式保留，脚本条件不会被臆测为真。
3. **岗位数量与就业产出分别观察。** 建筑提供岗位不等于已有相应人口就业，也不等于输入资源能够维持生产。4.5.2 的原始人口/岗位数值需要结合版本常量理解，不能套旧版单位。
4. **舰体数据与完整设计成本分别查询。** 组件、区段、可用科技和当前设计会影响成本与战斗表现。只读取舰体不能得出整舰价格或战斗保证。
5. **实际订单需要新状态验收。** 精确目标、正常付款、未完成项目和真实队列是执行证据；资料条目不构成操作成功。

## 在本机更新

```powershell
python -m stellaris_autopilot.knowledge --game-dir 'D:\steam\steamapps\common\Stellaris' --game-version 'Cygnus v4.5.2' --output qs-game-knowledge.json
```

可加 `--wiki-pages pages.json` 导入页面导出数组。每条包含 `title`、`url`、`revision`、`license`、`version`、`text`；目前接受官方 Wiki 和灰机 Wiki 的原始页面链接。

资料包用于本机查询。游戏升级、安装修改数值的 Mod 后应重新编译并核对版本；来源文件哈希供追溯，当前服务不会自动宣称旧资料适用于新的 Mod 配置。MCP 配置版本不匹配时拒绝查询。
