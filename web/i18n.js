/* English and Ukrainian strings, and which one the visitor reads.
 *
 * A string is text, or a function of its variables where the word order or
 * the count changes it. Ukrainian counts sit after a colon ("поїздок: 12"),
 * which reads naturally and needs no plural rules.
 */

const LANGS = ["en", "uk"];

const I18N = {
  en: {
    title: "Lviv bus ride times — delay on every stop-to-stop leg, by hour",
    description:
      "How long Lviv's buses take between stops, and how much longer at rush hour. Every stop-to-stop leg timed on archived GTFS-RT vehicles, dwell and lights included, coloured against its own quickest hour. Hour-of-day slider, month picker, weekday/weekend split, and ride time between any two points.",
    h1: "Lviv bus ride times",
    loading: "loading…",
    month: "Month",
    days: "Days",
    hour: "Hour",
    wholeDay: "Whole day",
    statistic: "Statistic",
    credit: "Source, method & contact on GitHub",
    rideTime: "Ride time",
    clear: "Clear",
    rulerNote:
      "Real vehicles, timed between your two ends — anywhere on a route, not just at its stops. Standing at stops and waiting at lights is included; waiting for the bus to turn up is not. Averages add up to a journey and medians do not, so a long ride reads about a tenth quicker on the median. Drag either end to move it, Esc to clear.",
    lanesH: "Bus lane candidates",
    lanesNote:
      "Weekday rush hours (07–09, 16–18): stretches of 300 m or more that run at least a quarter slower than at weekday midday (10–15), ranked by bus time lost per km of lane — rides × seconds over midday. Where routes join or split, a stretch follows the busiest one. Click one to see it.",
    lanesNone: "No stretch qualifies this month.",
    lanesLost: ({ min }) => `${min} bus-min/day`,
    lanesPerKm: ({ min }) => `${min} bus-min/km/day`,
    lanesBuses: ({ n }) => `${n} buses/h`,
    langName: "English",

    // Crawlable and screen-reader text.
    aboutH: "What this map shows",
    aboutP:
      "How long Lviv city buses take to get from one stop to the next, and how much longer that takes at a given hour than at the quickest hour of the day. Every stop-to-stop leg of the network is drawn along the street the buses drive, nudged to the right of its direction of travel so the two directions of a street lie side by side. Green means the leg runs about as quick as it ever does; red means it takes twice as long or more. Use the hour slider to compare the morning peak against the evening, the month picker to compare months, and the day selector to separate weekdays from weekends.",
    delayH: "Delay against off-peak",
    delayP:
      "Each leg is compared only with itself: its off-peak time is its quickest hour of the day with at least ten timed rides. That separates congestion from distance — a long leg on the edge of town is not red for being long, and a short one in the centre is red only if it takes much longer at rush hour than it does at its quietest. Where several routes share a leg, their times are pooled, weighted by how many rides each contributed. Clicking a leg shows its ride time, its off-peak time and hour, every route that drives it, and its ride time hour by hour. The Statistic selector opens on the median, which is how a leg usually goes; the average is there too.",
    pointsH: "Ride time between two points",
    pointsP:
      "Drop a point where you are and another where you are going, and the map lists every bus route that drives past both — in that order, with an arrow showing which way — timed on what its vehicles actually did between those two points. Neither end has to be a stop: each point is placed on the route's own line, so a spot halfway down a block is timed as a spot halfway down a block. The figures come from the same pass over the archive that follows individual buses between consecutive stops, so they include the dwell at each stop and the wait at each light. Picking a route draws the stretch of it being timed. Waiting for the bus to turn up is not measured: the archive records where buses were, not when the next one leaves.",
    howH: "How the figures are produced",
    howP:
      "Only buses are counted. Lviv trolleybuses are published in the schedule feed as <code>route_type=3</code>, the same code as buses, so they are excluded by route name; trams are excluded by route type. A leg is timed from the moment a vehicle comes within the stop radius of one stop on its own trip to the moment it reaches the next, so standing at the stop and waiting at lights are part of the leg. A leg seen on fewer than five rides in a slot is left out rather than guessed.",
    sourceP:
      "Source data: the Lviv public transport GTFS-RT feed, archived to object storage. Basemap: OpenStreetMap via Protomaps.",

    // Built by app.js.
    daytype: { all: "All days", wd: "Weekdays", we: "Weekends" },
    metric: { med: "Median", avg: "Average" },
    dayCount: ({ n }) => `${n}d`,
    longer: "longer than off-peak",
    allHours: "all hours",
    all: "all",
    legsInView: ({ n }) => `${n} legs in view`,
    noData: ({ msg }) => `no data for this selection (${msg})`,
    failed: ({ msg }) => `failed to load: ${msg}`,
    note: ({ first, last, n }) =>
      `Buses only. Each leg is timed stop to stop on the archived vehicles, standing at stops and waiting at lights included, and coloured against its own quickest hour of the day. ${first} … ${last}, ${n} timed legs.`,
    sec: "s",
    min: "min",
    hr: "h",
    km: "km",
    sparkLabel: "Ride time by hour of day",
    offPeak: "Off-peak",
    offPeakAt: ({ hour }) => `Off-peak (${hour}:00)`,
    delay: "Delay",
    tooFew: "too few rides",
    rides: ({ n }) => `${n} rides`,
    legSummary: ({ label, n }) => `${label}, ${n} rides, stop to stop incl. dwell`,
    againstOffPeak: ({ hour }) => `against off-peak, ${hour}`,
    quickestHour: "quickest hour of the day",
    offPeakTime: ({ hour, time }) => `off-peak ${hour}: ${time}`,
    ridesSeen: ({ n }) => `${n} rides seen`,
    estimated: ({ holes, legs }) => `${holes} of ${legs} legs estimated`,
    noRideTimes: "This build carries no measured ride times.",
    clickFirst: "Click the map to set where you are.",
    clickSecond: "Click again for where you are going. Drag either end to move it.",
    looking: "looking for buses…",
    noRoute: ({ m }) =>
      `No bus runs from the first end to the last one — no route passes within ${m} m of both, in that direction. Try the other way round.`,
    showSlower: ({ n }) => `Show ${n} slower route${n === 1 ? "" : "s"}`,
    showFewer: "Show fewer",
    onTheBus: "On the bus",
    ofObserved: ({ source }) => `${source} of observed rides`,
    rulerTitle: "Measure ride time between two points",
    rulerLabel: "Measure ride time",
    zoomIn: "Zoom in",
    hideControls: "Hide controls",
    showControls: "Show controls",
    zoomOut: "Zoom out",
  },

  uk: {
    title: "Час поїздки автобусами Львова — затримка на кожному перегоні, по годинах",
    description:
      "Скільки автобуси Львова їдуть між зупинками і наскільки довше в час пік. Кожен перегін виміряно за архівом GTFS-RT, разом зі стоянням на зупинках і світлофорах, і пофарбовано відносно його власної найшвидшої години. Повзунок години, вибір місяця, будні й вихідні окремо та час поїздки між будь-якими двома точками.",
    h1: "Час поїздки автобусами Львова",
    loading: "завантаження…",
    month: "Місяць",
    days: "Дні",
    hour: "Година",
    wholeDay: "Увесь день",
    statistic: "Показник",
    credit: "Код, методика й контакти на GitHub",
    rideTime: "Час поїздки",
    clear: "Очистити",
    rulerNote:
      "Справжні автобуси, виміряні між вашими двома точками — будь-де на маршруті, не лише на зупинках. Стояння на зупинках і світлофорах враховано, а очікування автобуса — ні. Середні складаються в цілу поїздку, а медіани — ні, тож довга поїздка за медіаною виходить приблизно на десяту частину коротшою. Перетягніть будь-яку точку, щоб її посунути; Esc — очистити.",
    lanesH: "Кандидати на смугу для громадського транспорту",
    lanesNote:
      "Будні години пік (07–09, 16–18): ділянки від 300 м, де їдуть щонайменше на чверть повільніше, ніж у будній полудень (10–15), за втраченим часом автобусів на кілометр смуги — поїздки × секунди понад полудень. Де маршрути сходяться чи розходяться, ділянка йде за найнавантаженішим. Натисніть, щоб побачити на карті.",
    lanesNone: "Цього місяця таких ділянок немає.",
    lanesLost: ({ min }) => `${min} хв автобусів/день`,
    lanesPerKm: ({ min }) => `${min} хв/км/день`,
    lanesBuses: ({ n }) => `автобусів/год: ${n}`,
    langName: "Українська",

    aboutH: "Що показує ця карта",
    aboutP:
      "Скільки міські автобуси Львова їдуть від однієї зупинки до наступної і наскільки довше це триває в обрану годину, ніж у найшвидшу годину доби. Кожен перегін мережі намальовано вздовж вулиці, якою їдуть автобуси, зі зсувом праворуч від напрямку руху, тож два напрямки однієї вулиці лежать поруч. Зелений — перегін їдеться майже так швидко, як буває взагалі; червоний — удвічі довше або більше. Повзунком години порівнюйте ранковий пік із вечірнім, вибором місяця — місяці між собою, а вибором днів відділяйте будні від вихідних.",
    delayH: "Затримка відносно години поза піком",
    delayP:
      "Кожен перегін порівнюється лише сам із собою: його час поза піком — це найшвидша година доби, в якій виміряно щонайменше десять поїздок. Так затори відділено від відстані — довгий перегін на околиці не червоний через те, що він довгий, а короткий у центрі червоний лише тоді, коли в пік він триває значно довше, ніж у найтихішу годину. Якщо перегін спільний для кількох маршрутів, їхні часи об'єднано зі вагою за кількістю поїздок. Клік по перегону показує його час, час і годину поза піком, кожен маршрут, що ним їде, і час поїздки по годинах. Показник за замовчуванням — медіана, тобто як перегін їдеться зазвичай; середнє теж є.",
    pointsH: "Час поїздки між двома точками",
    pointsP:
      "Поставте точку там, де ви є, і ще одну там, куди їдете, — карта покаже кожен автобусний маршрут, що проходить повз обидві в такому порядку (стрілка показує напрямок), з часом, який його автобуси справді витратили між цими точками. Жодна з точок не мусить бути зупинкою: кожну прив'язано до лінії самого маршруту, тож місце посеред кварталу вимірюється як місце посеред кварталу. Цифри дає той самий прохід архівом, що стежить за окремими автобусами між сусідніми зупинками, тому вони враховують стояння на кожній зупинці й кожному світлофорі. Вибраний маршрут малює на карті ділянку, яку виміряно. Очікування автобуса не виміряно: архів знає, де були автобуси, а не коли рушить наступний.",
    howH: "Як отримано цифри",
    howP:
      "Враховано лише автобуси. Тролейбуси Львова в розкладі мають <code>route_type=3</code>, той самий код, що й автобуси, тому їх відкинуто за назвою маршруту; трамваї відкинуто за типом маршруту. Перегін вимірюється від моменту, коли машина на своєму рейсі входить у радіус однієї зупинки, до моменту, коли досягає наступної, тож стояння на зупинці й світлофорах — частина перегону. Перегін, який у певній комірці бачили менше ніж на п'яти поїздках, не показано, а не вгадано.",
    sourceP:
      "Дані: GTFS-RT-потік громадського транспорту Львова, збережений в об'єктне сховище. Підкладка: OpenStreetMap через Protomaps.",

    daytype: { all: "Усі дні", wd: "Будні", we: "Вихідні" },
    metric: { med: "Медіана", avg: "Середнє" },
    dayCount: ({ n }) => `${n} дн.`,
    longer: "довше, ніж поза піком",
    allHours: "усі години",
    all: "усі",
    legsInView: ({ n }) => `перегонів на екрані: ${n}`,
    noData: ({ msg }) => `немає даних для цього вибору (${msg})`,
    failed: ({ msg }) => `не вдалося завантажити: ${msg}`,
    note: ({ first, last, n }) =>
      `Лише автобуси. Кожен перегін виміряно від зупинки до зупинки за архівом руху машин, разом зі стоянням на зупинках і світлофорах, і пофарбовано відносно його власної найшвидшої години доби. ${first} … ${last}, виміряних перегонів: ${n}.`,
    sec: "с",
    min: "хв",
    hr: "год",
    km: "км",
    sparkLabel: "Час поїздки за годинами доби",
    offPeak: "Поза піком",
    offPeakAt: ({ hour }) => `Поза піком (${hour}:00)`,
    delay: "Затримка",
    tooFew: "замало поїздок",
    rides: ({ n }) => `поїздок: ${n}`,
    legSummary: ({ label, n }) => `${label}, поїздок: ${n}, від зупинки до зупинки разом зі стоянням`,
    againstOffPeak: ({ hour }) => `відносно години поза піком, ${hour}`,
    quickestHour: "найшвидша година доби",
    offPeakTime: ({ hour, time }) => `поза піком ${hour}: ${time}`,
    ridesSeen: ({ n }) => `поїздок: ${n}`,
    estimated: ({ holes, legs }) => `оцінено ${holes} з ${legs} перегонів`,
    noRideTimes: "У цій збірці немає виміряних часів поїздки.",
    clickFirst: "Клацніть на карті там, де ви є.",
    clickSecond: "Клацніть ще раз там, куди їдете. Будь-яку точку можна перетягнути.",
    looking: "шукаю автобуси…",
    noRoute: ({ m }) =>
      `Від першої точки до другої автобус не їде — жоден маршрут не проходить ближче ніж за ${m} м від обох у цьому напрямку. Спробуйте навпаки.`,
    showSlower: ({ n }) => `Показати повільніші маршрути: ${n}`,
    showFewer: "Згорнути",
    onTheBus: "В автобусі",
    ofObserved: ({ source }) => `${source} спостережених поїздок`,
    rulerTitle: "Виміряти час поїздки між двома точками",
    rulerLabel: "Виміряти час поїздки",
    zoomIn: "Наблизити",
    hideControls: "Сховати панель",
    showControls: "Показати панель",
    zoomOut: "Віддалити",
  },
};

const LANG_KEY = "lang";

// A shared link's language wins, then the visitor's last pick, then the
// browser. Russian-language browsers get Ukrainian, never a third language.
function pickLang() {
  const asked = new URLSearchParams(location.search).get("lang");
  if (LANGS.includes(asked)) return asked;
  try {
    const saved = localStorage.getItem(LANG_KEY);
    if (LANGS.includes(saved)) return saved;
  } catch {}
  const browser = (navigator.languages || [navigator.language || ""]).map((l) =>
    l.toLowerCase().slice(0, 2),
  );
  return browser.find((l) => ["uk", "ru", "be"].includes(l)) ? "uk" : "en";
}

let LANG = pickLang();

function t(key, vars) {
  const s = I18N[LANG][key] ?? I18N.en[key];
  return typeof s === "function" ? s(vars) : s;
}

function locale() {
  return LANG === "uk" ? "uk-UA" : "en-GB";
}

function num(n) {
  return n.toLocaleString(locale());
}

// "May 2026" / "Травень 2026", from the month key rather than the build's label.
function monthName(key) {
  const [y, m] = key.split("-").map(Number);
  const s = new Intl.DateTimeFormat(locale(), {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  })
    .format(new Date(Date.UTC(y, m - 1, 1)))
    .replace(/\s*р\.$/, "");
  return s[0].toUpperCase() + s.slice(1);
}

// The page's own markup: data-i18n sets text, data-i18n-html markup.
function translatePage() {
  document.documentElement.lang = LANG;
  document.title = t("title");
  document.querySelector('meta[name="description"]').content = t("description");
  for (const el of document.querySelectorAll("[data-i18n]")) el.textContent = t(el.dataset.i18n);
  for (const el of document.querySelectorAll("[data-i18n-html]")) el.innerHTML = t(el.dataset.i18nHtml);
  for (const b of document.querySelectorAll("[data-lang]")) {
    b.setAttribute("aria-pressed", String(b.dataset.lang === LANG));
  }
}

function setLang(lang) {
  LANG = lang;
  try {
    localStorage.setItem(LANG_KEY, lang);
  } catch {}
  translatePage();
}

translatePage();
