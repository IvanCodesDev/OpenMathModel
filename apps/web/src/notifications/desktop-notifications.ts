/**
 * 桌面通知：长任务完成或需要确认时发系统通知。
 *
 * 三个前提缺一不可——设置里开关打开、浏览器支持 Notification、用户已授权。
 * 权限只能在用户手势里申请，因此申请动作绑定在设置开关上，而不是页面加载时。
 */

import { t } from "../i18n/locale";

const SETTINGS_KEY = "openmathmodelSettings";

export interface DesktopNotice {
  title: string;
  body: string;
  /** 同一 tag 只提醒一次，避免快照刷新时重复弹窗。 */
  tag: string;
  /** 点击通知后跳转的站内地址。 */
  url?: string;
  /** 开关刚打开时的确认提醒：即使用户正看着页面也应该出现。 */
  ignoreFocus?: boolean;
}

const delivered = new Set<string>();

export function notificationsSupported(): boolean {
  return typeof window !== "undefined" && "Notification" in window;
}

export function notificationPermission(): NotificationPermission {
  return notificationsSupported() ? Notification.permission : "denied";
}

export function desktopNotificationsEnabled(): boolean {
  try {
    const settings = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") as Record<string, unknown>;
    return settings.desktopNotifications === true;
  } catch {
    return false;
  }
}

/** 在用户手势中调用；已授权或已拒绝时直接返回当前状态。 */
export async function requestNotificationPermission(): Promise<NotificationPermission> {
  if (!notificationsSupported()) return "denied";
  if (Notification.permission !== "default") return Notification.permission;
  try {
    return await Notification.requestPermission();
  } catch {
    return Notification.permission;
  }
}

function userIsWatching(): boolean {
  return document.visibilityState === "visible" && document.hasFocus();
}

/** 返回是否真的弹出了通知，便于调用方决定是否改用页内提示。 */
export function sendDesktopNotification(notice: DesktopNotice): boolean {
  if (!notificationsSupported() || !desktopNotificationsEnabled()) return false;
  if (Notification.permission !== "granted") return false;
  // 用户正盯着页面时不打扰：界面本身已经把状态变化呈现出来了
  if (!notice.ignoreFocus && userIsWatching()) return false;
  if (delivered.has(notice.tag)) return false;

  try {
    const notification = new Notification(notice.title, {
      body: notice.body,
      tag: notice.tag,
      icon: "/assets/OpenMathModel_IP_Face.png",
    });
    delivered.add(notice.tag);
    notification.onclick = () => {
      window.focus();
      if (notice.url) window.location.href = notice.url;
      notification.close();
    };
    return true;
  } catch {
    return false;
  }
}

/** 设置里刚打开开关时的确认提醒。 */
export function sendNotificationPreview(): void {
  delivered.delete("omm-preview");
  sendDesktopNotification({
    title: t("桌面通知已开启"),
    body: t("长任务完成或需要确认时，OpenMathModel 会在这里提醒你。"),
    tag: "omm-preview",
    ignoreFocus: true,
  });
}

/** 运行状态变化提醒；`previous` 为空表示首次加载，不打扰用户。 */
export function notifyRunStatusChange(input: {
  runId: string;
  projectId: string;
  projectName: string;
  previous: string | undefined;
  current: string;
}): void {
  const { runId, projectId, projectName, previous, current } = input;
  if (!previous || previous === current) return;

  const url = `/task/running?run_id=${encodeURIComponent(runId)}&project_id=${encodeURIComponent(projectId)}`;
  const notices: Record<string, { title: string; body: string }> = {
    WAITING_APPROVAL: {
      title: t("需要你确认"),
      body: `${projectName}：${t("Agent 已暂停并等待确认后继续。")}`,
    },
    COMPLETED: {
      title: t("任务已完成"),
      body: `${projectName}：${t("全部阶段已完成，可以查看成果。")}`,
    },
    FAILED: {
      title: t("任务执行失败"),
      body: `${projectName}：${t("运行中断，可在页面上重试当前阶段。")}`,
    },
  };

  const notice = notices[current];
  if (!notice) return;
  sendDesktopNotification({ ...notice, tag: `omm-run-${runId}-${current}`, url });
}
