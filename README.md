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

本科选课的 `CourseSelectionAssistant`、协议模型、WebVPN/访问策略、Hy2 管理代码、全校开课查询与自动选课实现已经迁入本仓库，不再 import 宿主的 `fuckclassroom.course_selection` 包。宿主仍提供认证/凭据、Plugin API、Process Host 与当前 Hy2 原生资源路径等基础设施。

## 开发

开发分支为 `plugin-management`。合并到 `main` 后，CI 成功会自动发布 Registry v1 beta Release。
