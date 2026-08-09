import type { Metadata } from 'next';

export const metadata: Metadata = {
  referrer: 'no-referrer',
};

export default function ResetPasswordLayout({ children }: { children: React.ReactNode }) {
  return children;
}
