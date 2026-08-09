import type { Metadata } from 'next';
import localFont from 'next/font/local';
import './globals.css';
import { cn } from '@/lib/utils';
import { Providers } from './providers';

const geist = localFont({
  src: './fonts/GeistVF.woff',
  weight: '100 900',
  variable: '--font-sans',
  display: 'swap',
});

export const metadata: Metadata = {
  title: 'MAP — Marketplace Automation Platform',
  description: 'Автоматизация публикации товаров на Avito. SaaS B2B платформа.',
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="ru" suppressHydrationWarning>
      <body className={cn('min-h-screen bg-background font-sans antialiased', geist.variable)}>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
