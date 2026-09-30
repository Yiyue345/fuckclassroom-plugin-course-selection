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

第一阶段仍复用宿主提供的 `fuckclassroom.course_selection` 底层兼容层（WebVPN、Hy2、选课协议、自动任务实现）；插件 UI、生命周期、Worker facade、路由、模板与静态资源已经独立到本仓库。

## 开发

开发分支为 `plugin-management`。合并到 `main` 后，CI 成功会自动发布 Registry v1 beta Release。
