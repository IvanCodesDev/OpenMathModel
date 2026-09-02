/**
 * 桌面通知：长任务完成或需要确认时发系统通知。
 *
 * 三个前提缺一不可——设置里开关打开、浏览器支持 Notification、用户已授权。
 * 权限只能在用户手势里申请，因此申请动作绑定在设置开关上，而不是页面加载时。
 */

import { t } from "../i18n/locale";
import { notifySecurityEnabled, notifyTaskDoneEnabled } from "../preferences/privacy-preferences";

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

/** 运行状态变化提醒；`previous` 为空表示首次加载，不打扰用户。
 *
 *  同一运行会多次进入同一状态：G2 数据闸门与 G1 方案门先后 WAITING_APPROVAL、
 *  失败→重试→再失败、修订回合（ADR-0013）第 2 轮再次 COMPLETED。tag 只由
 *  runId+状态拼成的话，第二次进入会被 `delivered` 与浏览器的同 tag 去重双双
 *  吞掉——用户在别的标签页等着，永远收不到第二道门的提醒。因此 tag 还要带上
 *  「这一次进入」的标识：等待确认用审批 id（每道门一个），其余状态用最新事件
 *  序号（同一次完成的重复快照序号相同，第 2 轮完成序号必然更大）。 */
export function notifyRunStatusChange(input: {
  runId: string;
  projectId: string;
  projectName: string;
  previous: string | undefined;
  current: string;
  /** 当前待确认审批的 id；非等待确认状态传 null。 */
  approvalId?: string | null;
  /** 快照里最新事件的序号；没有事件时为 null。 */
  eventSequence?: number | null;
}): void {
  const { runId, projectId, projectName, previous, current, approvalId, eventSequence } = input;
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
  // 「数据与隐私」的任务完成通知开关：只约束完成/失败提醒；
  // 等待确认是流程卡点，仍由总的桌面通知开关控制。
  if ((current === "COMPLETED" || current === "FAILED") && !notifyTaskDoneEnabled()) return;
  sendDesktopNotification({
    ...notice,
    tag: runStatusNotificationTag(runId, current, { approvalId, eventSequence }),
    url,
  });
}

/** 运行状态提醒的去重 tag：同一运行第 N 次进入同一状态各自成一条。
 *  单独导出是为了让这条规则能被 node --test 直接覆盖（web 包没有 DOM 测试栈）。 */
export function runStatusNotificationTag(
  runId: string,
  status: string,
  occurrence: { approvalId?: string | null; eventSequence?: number | null } = {},
): string {
  const marker = status === "WAITING_APPROVAL" && occurrence.approvalId
    ? occurrence.approvalId
    : occurrence.eventSequence;
  const suffix = marker === null || marker === undefined || marker === "" ? "" : `-${marker}`;
  return `omm-run-${runId}-${status}${suffix}`;
}

/** 账户安全事件提醒（密码、双重验证、登录设备变化）；隐私开关关闭时静默。 */
export function notifySecurityChange(body: string): void {
  if (!notifySecurityEnabled()) return;
  sendDesktopNotification({
    title: t("账户安全提醒"),
    body,
    tag: `omm-security-${Date.now()}`,
    ignoreFocus: true,
  });
}
