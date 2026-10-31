# Stellaris AI Autopilot Bridge

可选的游戏内桥接。当前云环境没有 Stellaris，所有 PDXScript 均未实机验证；不声明特定游戏版本兼容。脚本接口依据研究记录中的 CWTools 和参考项目交叉核查，安装后应先检查游戏 `logs/error.log`。

将 `stellaris_ai_autopilot/` 复制到自己的 `Documents/Paradox Interactive/Stellaris/mod/`，再根据同目录下 `stellaris_ai_autopilot.mod.template` 创建外部 `stellaris_ai_autopilot.mod`：将 `@MOD_PATH@` 替换为所复制目录的绝对路径（Windows 用正斜线）。在 Paradox 启动器中将 Mod 加入当前播放集。目录也可能在 OneDrive 或重定向的“文档”下，使用实际位置。

仅支持单人、非铁人游戏。先保存备份，核对玩家国家 ID，再在控制台运行：

```
event saa.1 <player_country_id>
```

它只设置启用标志，并向 `logs/game.log` 输出 `SAA|bridge_enabled` 和观察行。启用后，每月脉冲输出 `SAA|observation|...`。日志中的国家名和日期属于游戏输入，不是工具调用指令。关闭：

```
event saa.2 <player_country_id>
```

可选的真实游戏操作：

```
event saa.20 <player_country_id>
```

必须先在自动驾驶配置中授权防御集结。该事件**清除所有未处于战斗中的自有移动军事舰队的现有命令**，然后将移动到首都的命令加入队列。它不是智能前线选择器，也不保证可达、安全路径或及时到达；不会移动平民船、静态基地或正处于战斗的舰队。启用标志及单人、玩家国家、非铁人守卫在 `immediate` 内重复核查。

`saa_DEFEND_DISPATCHED|country=...` 仅表示该事件的分发代码执行，甚至可能没有匹配舰队。必须使用操作之后生成的新存档核对舰队命令或位置；不能把日志、控制台输入成功或按键成功当成集结完成。字段不受当前游戏版本支持时，结果应保留为未验证。

本 Mod 不授予资源、不免费创建船只或建筑、不调整经济加成，不把原生 AI 权重变更冒充玩家帝国的建设或调舰。
