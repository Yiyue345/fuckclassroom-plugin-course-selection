# FuckClassroom 本科选课插件

FuckClassroom 的独立本科选课插件，插件 ID 为 `course_selection`。

## 功能

- 本科选课中心
- 全校开课查询
- 已选课程管理
- 定时/自动选课
- 教务会话与 WebVPN/Hy2 访问策略
- 接口记录与调试页面

## 依赖

- FuckClassroom: `>=0.1,<0.3`
- Plugin API: `1`
- Required host plugin: `core_ui`
- Python dependency: `playwright>=1.45`

本科选课的 `CourseSelectionAssistant`、协议模型、WebVPN/访问策略、Hy2 管理代码、Rust 源码、预编译 runner 发布链路、全校开课查询与自动选课实现均由本仓库维护，不再依赖宿主的 `fuckclassroom.course_selection` 包或 Hy2 原生资源路径。宿主只提供认证/凭据、Plugin API、Process Host、配置与数据目录等稳定基础能力。

## 开发

开发分支为 `plugin-management`。合并到 `main` 后，CI 成功会自动发布 Registry v1 beta Release。
