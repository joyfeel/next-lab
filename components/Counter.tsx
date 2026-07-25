"use client";

import { useState } from "react";

export function Counter() {
  const [count, setCount] = useState(0);
  const [step, setStep] = useState(1);

  return (
    <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
      <button onClick={() => setCount(count - step)}>-</button>
      <span style={{ minWidth: "3rem", textAlign: "center" }}>{count}</span>
      <button onClick={() => setCount(count + step)}>+</button>
      <label style={{ marginLeft: "1rem" }}>
        step:{" "}
        <input
          type="number"
          value={step}
          onChange={(e) => setStep(Number(e.target.value) || 1)}
          style={{ width: "4rem" }}
        />
      </label>
      <button onClick={() => setCount(0)}>reset</button>
    </div>
  );
}
