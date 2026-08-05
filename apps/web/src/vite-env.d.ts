/// <reference types="vite/client" />

// Phosphor 的 exports 别名直接指向 CSS 文件，TS 无法为其推导类型，
// 这里显式声明成副作用模块。
declare module "@phosphor-icons/web/regular";
declare module "@phosphor-icons/web/fill";
