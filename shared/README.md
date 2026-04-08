# Shared Context

Skill 间信息共享目录。

## 文件说明

| 文件 | 说明 |
|------|------|
| `context.yaml` | 当前环境上下文，包含服务地址、模型信息等 |

## 使用约定

### 写入方 (上游 Skill)

- 完成部署/启动后，将环境信息写入 `context.yaml`
- 必须更新 `metadata.created_by` 和 `metadata.updated_at`

### 读取方 (下游 Skill)

- 优先从 `context.yaml` 读取连接信息
- 如果 `context.yaml` 为空或不存在，提示用户手动配置

## 数据流

```
Container_Deploy  ──写入──>  context.yaml  ──读取──>  flagos-performance-testing
```
