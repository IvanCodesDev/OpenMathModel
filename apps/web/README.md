# OpenMathModel Web

React + Vite 实现的 OpenMathModel 产品前端，已覆盖原静态原型的 14 个页面和主要交互。

## 开发

```powershell
npm install
npm run dev
```

## 校验

```powershell
npm run check
npm run build
```

页面使用产品化路径，例如 `/projects`、`/workspace/data`、`/library/problems`；Vite history fallback 会把路径交给 React 路由入口。
