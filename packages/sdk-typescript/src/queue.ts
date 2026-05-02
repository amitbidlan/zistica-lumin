import { Span } from './span.js';

/** Bounded FIFO queue. Drops new items on overflow — never throws. */
export class BoundedQueue {
  private readonly items: Span[] = [];
  private droppedCount = 0;

  constructor(private readonly maxSize: number) {}

  put(span: Span): boolean {
    if (this.items.length >= this.maxSize) {
      this.droppedCount++;
      return false;
    }
    this.items.push(span);
    return true;
  }

  drain(maxItems?: number): Span[] {
    const limit = maxItems === undefined ? this.items.length : maxItems;
    return this.items.splice(0, limit);
  }

  get size(): number {
    return this.items.length;
  }

  get dropped(): number {
    return this.droppedCount;
  }
}
