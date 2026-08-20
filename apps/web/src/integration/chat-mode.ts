/**
 * 对话模式（composer「自动模式」按钮）：决定 Agent 回答的风格约束。
 *
 * 模式是本机偏好（localStorage，与模型选择同级），指令在发送时并入用户消息
 * 内容——服务端 /api/chat 保持无状态，五协议桥不需要新增字段。开场分析有
 * 自己的长度约束，不叠加模式指令。
 */

export interface ChatModeDefinition {
  id: "auto" | "research" | "rapid";
  label: string;
  /** 空字符串 = 不注入任何指令（自动模式）。 */
  instruction: string;
}

export const CHAT_MODES: ChatModeDefinition[] = [
  { id: "auto", label: "自动模式", instruction: "" },
  {
    id: "research",
    label: "深度研究",
    instruction:
      "【回答方式】深度研究模式：请系统、深入地分析，给出完整推理链路、关键假设、必要公式与验证思路，"
      + "分小节论述；宁可长而完整，不要省略关键论证。",
  },
  {
    id: "rapid",
    label: "快速分析",
    instruction:
      "【回答方式】快速分析模式：请直接给出要点式结论与最小必要论证，先结论后理由，"
      + "控制在几段以内，省略铺垫与展开。",
  },
];

const STORAGE_KEY = "openmathmodelChatMode";

export function currentChatMode(): ChatModeDefinition {
  let saved = "";
  try {
    saved = localStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    // 存储不可用时按自动模式处理。
  }
  return CHAT_MODES.find(mode => mode.id === saved) ?? CHAT_MODES[0];
}

export function saveChatMode(id: ChatModeDefinition["id"]): void {
  try {
    localStorage.setItem(STORAGE_KEY, id);
  } catch {
    // 保存失败时模式只在当前页面生效。
  }
}
