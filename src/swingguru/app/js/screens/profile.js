// Profile — identity, preferences, plan, help, about, account.
import { get } from "../api.js";
import { STUBS } from "../data/stubs.js";
import { badge, compactBar, esc, largeTitle, list, listRow, section, segmented, stat, stub, stubBadge, toast } from "../ui.js";

export const title = "Profile";

export async function render() {
  const stats = await get("profile");
  const fmt = (v) => (v == null ? "–" : v.toFixed(1));
  const user = STUBS.user;
  const initials = user.name.split(" ").map((p) => p[0]).join("").slice(0, 2);
  const since = new Date(user.memberSince).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  const pref = STUBS.preferences;
  return `${compactBar("Profile")}
  <div class="scroll">
    ${largeTitle("Profile")}
    ${stub(`<div class="card" style="text-align:center;padding:28px 20px 24px">
      <div class="avatar lg" style="margin:0 auto 14px">${esc(initials)}</div>
      <div class="headline" style="font-size:21px">${esc(user.name)}</div>
      <div class="footnote muted" style="margin-top:4px">Member since ${esc(since)}</div>
    </div>`, "user", { block: true })}
    <div class="stats" style="margin-top:12px">
      ${stat({ ico: "golf", value: String(stats.swings), label: "Swings", href: "#/library" })}
      ${stub(stat({ ico: "chart", value: fmt(stats.average.value), label: "Average score" }), "score", { on: stats.average.source === "stub", block: true })}
      ${stub(stat({ ico: "trophy", value: fmt(stats.best.value), label: "Best score" }), "score", { on: stats.best.source === "stub", block: true })}
    </div>
    <p class="footnote muted" style="margin:12px 4px 0">${stats.checkpoints_in_range.good} of ${stats.checkpoints_in_range.total} checkpoints in range across all your swings.</p>
    ${section("", list([
      listRow({ ico: "hand", label: "Handedness", sub: "Auto detects it from your set-up", control: segmented("handedness", [
        { value: "auto", label: "Auto" }, { value: "right", label: "Right" }, { value: "left", label: "Left" }], pref.handedness) }),
      listRow({ ico: "bell", label: "Practice reminders", control: segmented("reminders", [
        { value: "daily", label: "Daily" }, { value: "weekly", label: "Weekly" }, { value: "off", label: "Off" }], pref.reminders) }),
    ], "Preferences"))}
    ${section("", list([
      listRow({ ico: "sparkle", label: "Free plan", sub: "Unlimited analyses during the preview", right: stubBadge() }),
      listRow({ ico: "crown", label: "Swing Guru Pro", sub: "Programs, full history, priority analysis", tone: "accent", action: "upgrade", right: badge("7 days free") }),
      listRow({ ico: "restore", label: "Restore purchases", action: "restore" }),
    ], "Plan"))}
    ${section("", list([
      listRow({ ico: "camera", label: "Filming tips", action: "tips" }),
      listRow({ ico: "info", label: "How scores work", action: "score-help" }),
      listRow({ ico: "help", label: "Contact support", action: "support" }),
    ], "Help"))}
    ${section("", list([
      listRow({ ico: "user", label: "Sign in", sub: "Back up your swings across devices · coming soon", action: "signin" }),
    ], "Account"))}
    ${section("", list([
      listRow({ ico: "doc", label: "Version", value: STUBS.appVersion }),
      listRow({ ico: "doc", label: "Acknowledgements", value: "RTMW · OpenCV · OWLv2" }),
    ], "About"))}
    <div class="dev-only">${section("", list([
      listRow({ ico: "settings", label: "Design system", href: "#/system" }),
    ], "Developer"))}</div>
  </div>`;
}

export function mount(view) {
  view.querySelectorAll("[data-segmented]").forEach((group) =>
    group.addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      group.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
      STUBS.preferences[group.dataset.segmented] = btn.dataset.value;
      toast("Saved");
    }));
}
