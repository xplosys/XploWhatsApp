import { Logo } from "@/components/Logo";
import { ThemeToggle } from "@/components/ThemeToggle";

export default function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <main className="hero-band relative flex min-h-screen flex-col items-center justify-center px-4 py-12">
      <ThemeToggle className="absolute top-4 right-4" />
      <Logo className="mb-8 text-xl" tagline onDark />
      <div className="w-full max-w-md rounded-card border border-line bg-surface p-8 shadow-card">{children}</div>
      <p className="mt-8 text-xs text-[#8f99b8]">XploWhatsApp is a product of XploSys · Exploring simple and ideal solutions</p>
    </main>
  );
}
