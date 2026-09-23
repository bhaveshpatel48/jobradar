const SYMBOLS = { USD: '$', EUR: '€', GBP: '£', CAD: 'CA$', AUD: 'A$', INR: '₹', JPY: '¥' }
const PERIODS = { year: '/yr', month: '/mo', hour: '/hr' }

// 3750 -> "3.75k", 211400 -> "211.4k", 60000000 -> "60M", 65.38 -> "65.38".
// Number() drops trailing zeros, so round figures stay short ("180k").
function compact(n) {
  if (n >= 1_000_000) return `${Number((n / 1_000_000).toFixed(2))}M`
  if (n >= 1000) {
    const k = n / 1000
    return `${Number(k.toFixed(k < 10 ? 2 : 1))}k`
  }
  return String(Number(n.toFixed(2)))
}

// Prefer the numeric range (consistent formatting); fall back to the text as
// the board wrote it. Returns null when there's nothing to show.
export function formatSalary(job) {
  const { salary_min: min, salary_max: max, salary_currency: cur, salary_period: period } = job
  if (min || max) {
    const sym = SYMBOLS[cur] ?? (cur ? `${cur} ` : '')
    const range = min && max && min !== max ? `${compact(min)}–${compact(max)}` : compact(min || max)
    return `${sym}${range}${PERIODS[period] ?? ''}`
  }
  return job.salary_text || null
}
