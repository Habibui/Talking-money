/**
 * talkuyut-dengi v2 — внешний планировщик (Cloudflare Worker)
 * ============================================================
 *
 * 26.09.2026, поздний вечер. Дёргает GitHub Actions workflow_dispatch на
 * .github/workflows/publish_v2.yml в этом репозитории — почасово в
 * режиме collect, и два раза в день (08:00/19:00 МСК) в режиме issue.
 *
 * ЭТО ОТДЕЛЬНЫЙ, НОВЫЙ Worker — не правка существующего Worker'а v1
 * (тот дёргает publish.yml каждые ~15 минут и его исходный код не виден
 * в этом репозитории, поэтому трогать его отсюда нельзя не глядя). Заведи
 * второй, отдельный Worker специально под v2 — см. README.md, раздел
 * «Настройка Cloudflare Worker для v2», там точные шаги по кнопкам.
 *
 * Нужен один секрет Worker'а (Settings -> Variables and Secrets ->
 * Add -> Type: Secret): GITHUB_TOKEN — Personal Access Token с правом
 * "Actions: Read and write" на этот репозиторий (см. README.md, как
 * получить). Тот же PAT, что уже стоит у Worker'а v1, подойдёт и сюда,
 * если он даёт это же право на этот же репозиторий — но это ДВА разных
 * Worker'а, секрет всё равно нужно завести здесь отдельно (Cloudflare не
 * делится секретами между Worker'ами автоматически).
 *
 * Cron Triggers (Settings -> Triggers -> Cron Triggers), ВСЕ в UTC —
 * Cloudflare Cron Triggers не понимают часовых поясов, а МСК = UTC+3
 * круглый год (без перехода на летнее время):
 *   "0 * * * *"    -> mode=collect   (почасово, в начале каждого часа)
 *   "5 5 * * *"    -> mode=issue     (05:05 UTC = 08:05 МСК)
 *   "5 16 * * *"   -> mode=issue     (16:05 UTC = 19:05 МСК)
 *
 * Сдвиг issue-триггеров на 5 минут от часового collect — запас на всякий
 * случай (сам Worker может слегка задержаться под нагрузкой Cloudflare),
 * не жёсткое требование: mode=issue в самом пайплайне (scripts/issue_v2.py)
 * теперь САМ первым шагом гоняет Сборщик перед тем, как формировать выпуск,
 * так что корректность выпуска не зависит от того, успел ли отработать
 * соседний почасовой collect (см. комментарий в issue_v2.py и в
 * .github/workflows/publish_v2.yml).
 */

const OWNER = "Habibui";
const REPO = "Talking-money";
const WORKFLOW_FILE = "publish_v2.yml";

/** По какому cron-выражению определяем режим — держать в синхроне со
 * списком Cron Triggers в Settings -> Triggers этого Worker'а. */
function modeForCron(cron) {
  if (cron === "0 * * * *") return "collect";
  if (cron === "5 5 * * *" || cron === "5 16 * * *") return "issue";
  return null;
}

async function dispatchWorkflow(mode, env) {
  if (!env.GITHUB_TOKEN) {
    throw new Error("Секрет GITHUB_TOKEN не задан у этого Worker'а (Settings -> Variables and Secrets)");
  }

  const url = `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`;
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "talkuyut-dengi-v2-worker",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: "main", inputs: { mode } }),
  });

  if (!response.ok) {
    const body = await response.text();
    console.error(`GitHub API вернул ${response.status} на mode=${mode}: ${body}`);
    throw new Error(`workflow_dispatch(mode=${mode}) вернул ${response.status}`);
  }

  console.log(`workflow_dispatch(mode=${mode}) отправлен успешно (${response.status})`);
}

export default {
  // Основной путь — срабатывает по Cron Triggers, настроенным в дашборде
  // (Settings -> Triggers), см. комментарий про cron-строки в начале файла.
  async scheduled(event, env, ctx) {
    const mode = modeForCron(event.cron);
    if (!mode) {
      console.error(`Незнакомая cron-строка у этого срабатывания: "${event.cron}" — не знаю, какой mode дёргать. Проверь, что здесь (modeForCron) и в Settings -> Triggers указаны одни и те же три cron-строки.`);
      return;
    }
    await dispatchWorkflow(mode, env);
  },

  // Ручной запуск для проверки — необязателен для штатной работы (за неё
  // отвечает scheduled() выше), но сильно упрощает живой прогон в
  // воскресенье: открой в браузере (или curl)
  //   https://<имя-worker'а>.<твой-поддомен>.workers.dev/?mode=collect
  //   https://<имя-worker'а>.<твой-поддомен>.workers.dev/?mode=issue
  // и сразу увидишь, ушёл ли запуск в GitHub Actions, не дожидаясь cron.
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const mode = url.searchParams.get("mode");
    if (mode !== "collect" && mode !== "issue") {
      return new Response(
        "Добавь в адрес ?mode=collect или ?mode=issue, чтобы запустить вручную для проверки.",
        { status: 400 },
      );
    }
    try {
      await dispatchWorkflow(mode, env);
      return new Response(`OK — запуск mode=${mode} отправлен в GitHub Actions. Проверь вкладку Actions в репозитории.`, { status: 200 });
    } catch (err) {
      return new Response(`Ошибка: ${err.message}`, { status: 500 });
    }
  },
};
