**MCP（Model Context Protocol）是让 Agent 以统一方式接入外部工具的协议：连上 MCP 服务器 → 发现它有哪些工具 → 和本地工具登记进同一张表。**

依据（来源：agent-anatomy.md，本次回答沿用上一条的检索结果，未重复检索）：
- 对模型而言远端工具与本地工具没有区别，调用格式、报错处理一致，因此新增工具不需要改 Agent 核心。
- 工具来源可插拔：本地实现一部分，其余通过挂载 MCP 服务器获得。

例子：本会话的工具表里，`echo`、`add` 来自 `example` 这个 MCP 服务器，`calculator`、`read_file`、`search_knowledge_base` 是本地工具，我在同一轮调用里混着用没有任何差别——比如前面算 `1 + 1` 走的是本地 `calculator`。

不确定的部分：文档只写了 MCP 的定位，没写传输层（stdio/HTTP）、鉴权、工具发现的具体报文格式，这些我不臆测。
