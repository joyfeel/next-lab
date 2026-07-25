import { Counter } from "@/components/Counter";
import { TodoList } from "@/components/TodoList";

export default function Home() {
  return (
    <div>
      <h1>next-lab</h1>
      <p>Scratchpad for App Router experiments. Expect broken things.</p>

      <section>
        <h2>useState basics</h2>
        <Counter />
      </section>

      <section>
        <h2>Controlled inputs + list state</h2>
        <TodoList />
      </section>
    </div>
  );
}
