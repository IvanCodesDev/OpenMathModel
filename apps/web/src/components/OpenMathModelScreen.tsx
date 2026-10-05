import type { ReactNode } from "react";
import { useLayoutEffect } from "react";
import parse, { Element } from "html-react-parser";
import HomeMascot from "./HomeMascot";
import HomeIntro from "./HomeIntro";
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

  return parse(markup, screen === "new" ? {
    replace(node) {
      if (node instanceof Element && node.name === "img" && node.attribs.class?.split(/\s+/).includes("hero-logo")) {
        return <HomeMascot />;
      }
      if (node instanceof Element && node.parent instanceof Element && node.parent.attribs.class?.split(/\s+/).includes("new-screen")) {
        if (node.name === "h1") {
          const subtitle = node.parent.children.find(child => child instanceof Element && child.name === "p" && child.attribs.class?.split(/\s+/).includes("lead"));
          const textOf = (element: Element) => element.children.filter(child => child.type === "text").map(child => child.data).join("");
          if (subtitle instanceof Element) return <HomeIntro title={textOf(node)} subtitle={textOf(subtitle)} />;
        }
        if (node.name === "p" && node.attribs.class?.split(/\s+/).includes("lead")) return <></>;
      }
    },
  } : undefined);
}
