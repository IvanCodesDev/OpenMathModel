import type { ReactNode } from "react";
import { useLayoutEffect } from "react";
import parse from "html-react-parser";
import { t } from "../i18n/locale";
import { activateScreen, getScreenMarkup } from "../legacy/openmathmodel-ui";
import type { ScreenId } from "../types/screens";

interface OpenMathModelScreenProps {
  screen: ScreenId;
  title: string;
}

export default function OpenMathModelScreen({ screen, title }: OpenMathModelScreenProps): ReactNode {
  const markup = getScreenMarkup(screen);

  useLayoutEffect(() => {
    // 标题不在正文里，翻译器扫不到，这里显式走一次词典。
    document.title = `OpenMathModel · ${t(title)}`;
    activateScreen(screen);
  }, [screen, title]);

  // 合并工作台（B 方案）：五个阶段路由渲染同一份包含 contenteditable 论文编辑器的标记，
  // 统一走原始 HTML 注入，避免 html-react-parser 触碰可编辑区域。
  if (screen === "editor" || screen === "data" || screen === "model" || screen === "experiments" || screen === "complete") {
    return <div className="react-html-root" dangerouslySetInnerHTML={{ __html: markup }} />;
  }

  return parse(markup);
}
