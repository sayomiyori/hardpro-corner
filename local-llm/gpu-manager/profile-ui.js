const el = (id) => document.getElementById(id);
async function get(path) {
  const response = await fetch("/api/" + path, {cache: "no-store", signal: AbortSignal.timeout(45000)});
  if (!response.ok) throw Error("Не удалось получить состояние панели.");
  return response.json();
}
const context = el("flash-context");
try {
  const saved = localStorage.getItem("gpu-manager-context");
  if (/^(16|64|128)K(-NCCL)?$/.test(saved)) context.value = saved;
} catch {}
context.addEventListener("change", () => {
  try { localStorage.setItem("gpu-manager-context", context.value); } catch {}
});
const profileButtons = [
  "quick-work",
  "quick-qwen",
  "quick-video",
].map(el);
let planning = false,
  readingProfile = false,
  profileTimer,
  switching = false;
const jobOutput = el("profile-progress");
function profileButtonsState() {
  for (const button of profileButtons)
    button.disabled =
      planning || switching;
}
async function postProfile(data) {
  const session = await get("session");
  const response = await fetch("/api/profile", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Control-Token": session.token,
    },
    body: JSON.stringify(data),
    signal: AbortSignal.timeout(45000),
  });
  const result = await response.json();
  if (!response.ok)
    throw Error(result.error || "Не удалось проверить операцию.");
  return result;
}
function displayProfile(job) {
  switching = ["running", "queued"].includes(job.status);
  if (job.status !== "idle") {
    const memory = job.memory
      ? Object.entries(job.memory)
          .map(([i, v]) => "GPU " + i + " · " + v.toLocaleString("ru-RU") + " МиБ")
          .join(" / ")
      : "";
    jobOutput.textContent = [job.error, job.step, memory]
      .filter(Boolean)
      .join(" · ");
  } else if (!jobOutput.textContent)
    jobOutput.textContent = "Готов к проверке выбранной раскладки.";
  profileButtonsState();
  clearTimeout(profileTimer);
  if (switching) profileTimer = setTimeout(profileStatus, 1800);
}
async function profileStatus() {
  if (readingProfile) return;
  readingProfile = true;
  try {
    displayProfile(await get("profile-status"));
  } catch {
    jobOutput.textContent =
      "Не удалось получить состояние сервера. Проверка будет повторена.";
    clearTimeout(profileTimer);
    profileTimer = setTimeout(profileStatus, 3000);
  } finally {
    readingProfile = false;
  }
}
async function prepareProfile(layout) {
  if (planning || switching) return;
  planning = true;
  profileButtonsState();
  const [context, transport] = el("flash-context").value.split("-");
  const request = {
    layout: [...layout],
    flash: `Qwen3.8-Flash-Next-${context}-3GPU${transport === "NCCL" ? "-NCCL" : ""}`,
  };
  try {
    const plan = await postProfile({ ...request, mode: "plan" });
    const details = [
      "Остановить: " + (plan.stop.join(", ") || "ничего") + ".",
      "Запустить: " + (plan.start.join(", ") || "ничего") + ".",
      "Очереди и процессы будут проверены повторно.",
    ];
    if (plan.system.length)
      details.push(
        (transport === "NCCL" && layout.every((slot) => slot === "flash")
          ? "На время работы VM приостановится: " : "Остаётся системная нагрузка: ") +
          plan.system
            .map(
              ([i, , name, mib]) =>
                name + " на GPU " + i + " (" + mib + " МиБ)",
            )
            .join(", ") +
          ".",
      );
    jobOutput.textContent = details.join(" ");
    if (!window.confirm(details.join("\n"))) return;
    displayProfile(
      await postProfile({
        ...request,
        mode: "apply",
        signature: plan.signature,
        id: crypto.randomUUID(),
      }),
    );
  } catch (error) {
    jobOutput.textContent = error.message;
  } finally {
    planning = false;
    profileButtonsState();
  }
}
el("quick-work").onclick = () => prepareProfile(["manager", "coder", "comfy"]);
el("quick-qwen").onclick = () => prepareProfile(["flash", "flash", "flash"]);
el("quick-video").onclick = () => prepareProfile(["video", "video", "video"]);
profileStatus();
