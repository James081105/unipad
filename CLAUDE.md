# CLAUDE.MD

## 强制遵守的箴言

> 来自 *The Pragmatic Programmer*

1. Care About Your Craft
2. Think! About Your Work
3. You Have Agency
4. Provide Options, Don't Make Lame Excuses
5. Don't Live with Broken Windows
6. Be a Catalyst for Change
7. Remember the Big Picture
8. Make Quality a Requirements Issue
9. Invest Regularly in Your Knowledge Portfolio
10. Critically Analyze What You Read and Hear
11. English is Just Another Programming Language
12. It's Both What You Say and the Way You Say It
13. Build Documentation In, Don't Bolt It On
14. Good Design Is Easier to Change Than Bad Design
15. DRY—Don't Repeat Yourself
16. Make It Easy to Reuse
17. Eliminate Effects Between Unrelated Things
18. There Are No Final Decisions
19. Forgo Following Fads
20. Use Tracer Bullets to Find the Target
21. Prototype to Learn
22. Program Close to the Problem Domain
23. Estimate to Avoid Surprises
24. Iterate the Schedule with the Code
25. Keep Knowledge in Plain Text
26. Use the Power of Command Shells
27. Achieve Editor Fluency
28. Always Use Version Control
29. Fix the Problem, Not the Blame
30. Don't Panic
31. Failing Test Before Fixing Code
32. Read the Damn Error Message
33. "select" Isn't Broken
34. Don't Assume It—Prove It
35. Learn a Text Manipulation Language
36. You Can't Write Perfect Software
37. Design with Contracts
38. Crash Early
39. Use Assertions to Prevent the Impossible
40. Finish What You Start
41. Minimize Coupling Between Modules
42. Configure, Don't Integrate
43. Put Abstractions in Code, Details in Metadata
44. Analyze Workflow to Improve Concurrency
45. Design Using Services
46. Always Design for Concurrency
47. Separate Views from Models
48. If It's Important Enough To Be Global, Wrap It in an API
49. Programming Is About Code, But Programs Are About Data
50. Don't Hoard State; Pass It Around
51. Don't Pay Inheritance Tax
52. Prefer Interfaces to Express Polymorphism
53. Delegate to Services: Has-A Trumps Is-A
54. Use Mixins to Share Functionality
55. Parameterize Your App Using External Configuration
56. Analyze Workflow to Improve Concurrency
57. Shared State Is Incorrect State
58. Random Failures Are Often Concurrency Issues
59. Use Actors For Concurrency Without Shared State
60. Use Blackboards to Coordinate Workflow
61. Listen to Your Inner Lizard
62. Don't Program by Coincidence
63. Estimate the Order of Your Algorithms
64. Test Your Estimates
65. Refactor Early, Refactor Often
66. Testing Is Not About Finding Bugs
67. A Test Is the First User of Your Code
68. Build End-To-End, Not Top-Down or Bottom Up
69. Design to Test
70. Test Your Software, or Your Users Will
71. Use Property-Based Tests to Validate Your Assumptions
72. Keep It Simple and Minimize Attack Surfaces
73. Apply Security Patches Quickly
74. Name Well; Rename When Needed
75. No One Knows Exactly What They Want
76. Programmers Help People Understand What They Want
77. Requirements Are Learned in a Feedback Loop
78. Work with a User to Think Like a User
79. Policy Is Metadata
80. Use a Project Glossary
81. Don't Think Outside the Box—Find the Box
82. Don't Go Into the Code Alone
83. Agile Is Not a Noun; Agile Is How You Do Things
84. Maintain Small Stable Teams
85. Schedule It and It Will Happen
86. Deploy It and It Will Happen
87. Don't Use Manual Procedures
88. Code Generators Increase Your Productivity
89. Duplication Is the Primary Enemy of a Well-Designed System
90. Don't Use Wizard Code You Don't Understand
91. Test Early, Test Often, Test Automatically
92. Coding Ain't Done 'Til All the Tests Run
93. Use Saboteurs to Test Your Testing
94. Test State Coverage, Not Code Coverage
95. Find Bugs Once
96. Treat English as Just Another Programming Language
97. Sign Your Work
98. First, Do No Harm
99. Don't Enable Scumbags
100. It's Your Life. Share It. Celebrate It. Build It. And Have Fun!

---

## Git

- **Commit message 一律使用英语**
- 格式：

```text
<type>(<scope>): <subject>

<body>

<footer>
```

---

## WebSearch / WebFetch

- WebSearch 与 WebFetch 工具**已被主动禁用**
- 可使用 **exa mcp**

---

## CDP

- 需要与浏览器交互时，**直接使用 CDP MCP**
- **不允许**新建 profile

---

## Subagent 与 Teammate

| 维度           | Subagent（子代理）               | Teammate（队友）                             |
| :------------- | :------------------------------- | :------------------------------------------- |
| **运行模式**   | 单一会话内，任务结束即销毁       | 独立会话，长期运行直至任务完成               |
| **通信方式**   | 仅向主代理**单向汇报**结果       | 队友间**直接互发消息**，可绕过 Lead 与你交互 |
| **协调机制**   | 主代理**集中管理**所有工作       | **共享任务列表**，自我协调                   |
| **上下文**     | 独立窗口，结果摘要返回主会话     | 完全独立的上下文窗口                         |
| **最适合场景** | 焦点明确、只需结果的**辅助任务** | 需要讨论、协作与竞争的**复杂工作**           |

> 除极其简单的任务外，任务一律使用 subagent 或 teammates 提高效率；数量不限。

---

## 互联网发言准则

- 在**没有明确授权**（“以我的名义发送 xxx”）时，所有留在互联网上的信息，**必须注明由 agent 发布**

---

## 需要人类过目的内容

- 使用 `start` 命令打开文件供用户查看
- 理由：用户不会一直守在 agent 窗体前

---

## 五个审查

审查代码时必须进行反向审查、性能审查、规范审查、调用链审查、对质（即承重测试，改掉代码语义观察对应点的单元测试是否通过，如果过了就是坏代码；对质agent至多自主运行四个小时，超时了就需要主agent介入或人工介入）

### Teamwork 优先规则

- 复杂项目默认使用长期运行的 teamwork/teammates，不以一次性 subagent 作为主要协作方式。
- 允许创建不限数量的 teammates 和不限数量的 subagents；数量按任务需要动态扩展，不设人为上限。
- Teammates 负责持续分工、直接互相沟通、同步接口和测试；subagents 负责短期、边界清晰的辅助任务。
- 主代理必须负责集成、冲突解决、全量验证和最终审计，不能把成员的局部完成当作项目完成。
- 未达到完整 PRD 和证据门禁前，任何成员不得宣称任务完成。

### 代码阶段与现场阶段分离

- 开发环境当前为 Windows。必须先在本机完成 PRD 中所有可编码阶段（阶段 0 的文档与设计、阶段 1、阶段 2、阶段 3 的全部代码、测试、模拟测试、审计和回滚材料），再进入现场部署。
- 不得因为没有 Ubuntu、PostgreSQL/ClickHouse/NAS 实例或希沃一体机而停止代码工作；应使用 Windows 可运行的 fake/in-memory/test harness、协议模拟、故障注入、浏览器自动化和交叉编译完成可重复验证。
- 每个阶段必须分别记录四个字段：实现状态、Windows/模拟测试状态、现场验证状态、阻断原因。CODE COMPLETE 或 SIMULATION PASS 不得写成现场 PASS；缺少现场环境只能标记 UNVERIFIED/BLOCKED。
- 只有在所有代码阶段的实现、测试、审计、文档、回滚和证据均完成后，才可结束代码 Goal；不得把现场部署、试点、扩容和生产运维冒充为已完成，也不得以现场缺失为理由宣称代码未完成。
- 主智能体必须持续修复失败、运行全量验证、更新审计证据并整合 teamwork/subagents，直到代码阶段达到 CODE COMPLETE；最终报告必须列出现场未验证项。

