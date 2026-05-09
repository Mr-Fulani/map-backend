/**
 * Onboarding Wizard — 5-шаговый мастер настройки.
 *
 * Шаг 1: Подключение Avito (client_id + client_secret)
 * Шаг 2: Источник данных (1С HTTP / XML / CSV)
 * Шаг 3: Тест подключения + импорт 10 товаров
 * Шаг 4: Маппинг категорий 1С → Avito
 * Шаг 5: Первая синхронизация → готово
 */

'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/auth-context';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Progress } from '@/components/ui/progress';
import { Zap } from 'lucide-react';
import { StepAvito } from './steps/step-avito';
import { StepDatasource } from './steps/step-datasource';
import { StepTest } from './steps/step-test';
import { StepCategories } from './steps/step-categories';
import { StepSync } from './steps/step-sync';

const STEPS = [
  { id: 1, title: 'Подключение Avito', description: 'Введите API-ключи Avito' },
  { id: 2, title: 'Источник данных', description: 'Откуда берём товары' },
  { id: 3, title: 'Тест подключения', description: 'Проверяем всё работает' },
  { id: 4, title: 'Категории', description: 'Маппинг категорий' },
  { id: 5, title: 'Запуск', description: 'Первая синхронизация' },
];

export default function OnboardingPage() {
  const [currentStep, setCurrentStep] = useState(1);
  const [wizardData, setWizardData] = useState<Record<string, unknown>>({});
  const router = useRouter();
  const { isAuthenticated, isLoading } = useAuth();

  if (isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <div className="h-10 w-10 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>
    );
  }

  if (!isAuthenticated) {
    router.push('/login');
    return null;
  }

  const progress = (currentStep / STEPS.length) * 100;

  function handleNext(data?: Record<string, unknown>) {
    if (data) {
      setWizardData((prev) => ({ ...prev, ...data }));
    }
    if (currentStep < STEPS.length) {
      setCurrentStep(currentStep + 1);
    }
  }

  function handleBack() {
    if (currentStep > 1) {
      setCurrentStep(currentStep - 1);
    }
  }

  function handleFinish() {
    router.push('/dashboard');
  }

  const stepInfo = STEPS[currentStep - 1];

  return (
    <div className="flex min-h-screen flex-col bg-gradient-to-br from-background via-background to-muted/30">
      {/* Decorative */}
      <div className="pointer-events-none fixed inset-0 overflow-hidden">
        <div className="absolute -top-60 left-1/2 h-[500px] w-[500px] -translate-x-1/2 rounded-full bg-primary/3 blur-3xl" />
      </div>

      {/* Header */}
      <header className="relative border-b bg-card/50 backdrop-blur-sm">
        <div className="mx-auto flex max-w-3xl items-center justify-between px-4 py-4">
          <div className="flex items-center gap-2.5">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary">
              <Zap className="h-4 w-4 text-primary-foreground" />
            </div>
            <span className="text-lg font-bold">MAP</span>
          </div>
          <div className="text-sm text-muted-foreground">
            Шаг {currentStep} из {STEPS.length}
          </div>
        </div>
      </header>

      {/* Progress bar */}
      <div className="relative mx-auto w-full max-w-3xl px-4 pt-6">
        <Progress value={progress} className="h-2" />

        {/* Step indicators */}
        <div className="mt-3 flex justify-between">
          {STEPS.map((step) => (
            <div
              key={step.id}
              className={`flex flex-col items-center text-center ${
                step.id <= currentStep
                  ? 'text-primary'
                  : 'text-muted-foreground/50'
              }`}
            >
              <div
                className={`flex h-7 w-7 items-center justify-center rounded-full text-xs font-bold transition-colors ${
                  step.id < currentStep
                    ? 'bg-primary text-primary-foreground'
                    : step.id === currentStep
                      ? 'border-2 border-primary text-primary'
                      : 'border border-muted-foreground/30 text-muted-foreground/50'
                }`}
              >
                {step.id < currentStep ? '✓' : step.id}
              </div>
              <span className="mt-1 hidden text-xs sm:block">{step.title}</span>
            </div>
          ))}
        </div>
      </div>

      {/* Step content */}
      <div className="relative mx-auto w-full max-w-3xl flex-1 px-4 py-8">
        <Card>
          <CardHeader>
            <CardTitle>{stepInfo.title}</CardTitle>
            <CardDescription>{stepInfo.description}</CardDescription>
          </CardHeader>
          <CardContent>
            {currentStep === 1 && (
              <StepAvito
                data={wizardData}
                onNext={handleNext}
              />
            )}
            {currentStep === 2 && (
              <StepDatasource
                data={wizardData}
                onNext={handleNext}
                onBack={handleBack}
              />
            )}
            {currentStep === 3 && (
              <StepTest
                data={wizardData}
                onNext={handleNext}
                onBack={handleBack}
              />
            )}
            {currentStep === 4 && (
              <StepCategories
                data={wizardData}
                onNext={handleNext}
                onBack={handleBack}
              />
            )}
            {currentStep === 5 && (
              <StepSync
                data={wizardData}
                onFinish={handleFinish}
                onBack={handleBack}
              />
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
