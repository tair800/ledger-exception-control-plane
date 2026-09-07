import Link from "next/link";

import { ExceptionDetailView } from "@/components/exception-detail";

export default async function ExceptionPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <div className="space-y-4">
      <Link href="/" className="text-ink-dim hover:text-ink">
        &larr; Exception queue
      </Link>
      <ExceptionDetailView exceptionId={id} />
    </div>
  );
}
