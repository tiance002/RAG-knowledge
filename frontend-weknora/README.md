# RAG Knowledge 正式前端

使用 React、TypeScript 和 Vite。任务面板与文件请求去重组件位于 `src/file-tasks/`。

```bash
npm install
npm run dev
npm run build
```

开发页面使用端口 `3001`，Vite 将 `/api` 与 `/static` 转发到本机后端 `8000`。生产构建输出到 `dist/`，ECS 上由 Nginx 提供页面并代理后端。后端与 Worker 启动命令见 [仓库 README](../README.md)。
