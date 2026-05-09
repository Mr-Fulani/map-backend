/**
 * Dashboard главная — KPI виджеты (будет расширена в Этапе 14).
 */

export default function DashboardPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Дашборд</h1>
        <p className="text-muted-foreground">
          Обзор вашей платформы автоматизации маркетплейсов.
        </p>
      </div>

      {/* KPI cards will be added in Stage 14 */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <div className="rounded-xl border bg-card p-6">
          <p className="text-sm text-muted-foreground">Активные объявления</p>
          <p className="mt-2 text-3xl font-bold">—</p>
        </div>
        <div className="rounded-xl border bg-card p-6">
          <p className="text-sm text-muted-foreground">Товаров в каталоге</p>
          <p className="mt-2 text-3xl font-bold">—</p>
        </div>
        <div className="rounded-xl border bg-card p-6">
          <p className="text-sm text-muted-foreground">AI-кредиты</p>
          <p className="mt-2 text-3xl font-bold">—</p>
        </div>
        <div className="rounded-xl border bg-card p-6">
          <p className="text-sm text-muted-foreground">Ошибки (24ч)</p>
          <p className="mt-2 text-3xl font-bold">—</p>
        </div>
      </div>
    </div>
  );
}
