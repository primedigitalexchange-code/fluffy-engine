async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
    ...options,
  });

  const isJson = (response.headers.get("content-type") || "").includes("application/json");
  const payload = isJson ? await response.json() : null;
  if (!response.ok) {
    const detail = payload && payload.detail ? payload.detail : `Request failed: ${response.status}`;
    throw new Error(detail);
  }
  return payload;
}

async function handleLogin(event) {
  event.preventDefault();
  const errorNode = document.getElementById("login-error");
  errorNode.textContent = "";

  const username = document.getElementById("username").value;
  const password = document.getElementById("password").value;

  try {
    await fetchJson("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    window.location.assign("/dashboard");
  } catch (error) {
    errorNode.textContent = error.message;
  }
}

function renderAccounts(accounts) {
  const list = document.getElementById("accounts-list");
  list.innerHTML = "";
  if (!accounts.length) {
    list.innerHTML = "<li>No bank accounts connected yet.</li>";
    return;
  }

  accounts.forEach((account) => {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${account.bank_name || "Linked bank"} • ${account.masked_account_number} • $${account.balance} • ${account.status}`;
    button.addEventListener("click", () => loadTransactions(account.account_id));
    item.appendChild(button);
    list.appendChild(item);
  });
}

function renderTransactions(payload) {
  const list = document.getElementById("transactions-list");
  list.innerHTML = "";
  if (!payload.transactions.length) {
    list.innerHTML = `<li>No ${payload.source} transactions available.</li>`;
    return;
  }

  payload.transactions.forEach((transaction) => {
    const item = document.createElement("li");
    item.textContent = `${transaction.posted_on} • ${transaction.name} • $${transaction.amount}${transaction.pending ? " • pending" : ""}`;
    list.appendChild(item);
  });
}

async function loadAccounts() {
  const payload = await fetchJson("/bank/accounts");
  renderAccounts(payload.accounts);
}

async function loadTransactions(accountId) {
  const payload = await fetchJson(`/bank/accounts/${encodeURIComponent(accountId)}/transactions`);
  renderTransactions(payload);
}

async function connectBank() {
  const statusNode = document.getElementById("connect-status");
  statusNode.textContent = "Preparing Plaid Link…";

  try {
    const tokenPayload = await fetchJson("/bank/link-token", { method: "POST" });
    if (!window.Plaid) {
      throw new Error("Plaid Link failed to load from the CDN.");
    }
    const handler = window.Plaid.create({
      token: tokenPayload.link_token,
      onSuccess: async (publicToken) => {
        statusNode.textContent = "Linking account…";
        await fetchJson("/bank/exchange-token", {
          method: "POST",
          body: JSON.stringify({ public_token: publicToken }),
        });
        statusNode.textContent = "Bank account connected.";
        await loadAccounts();
      },
      onExit: () => {
        if (!statusNode.textContent) {
          statusNode.textContent = "Plaid Link closed.";
        }
      },
    });
    handler.open();
    statusNode.textContent = "Plaid Link opened.";
  } catch (error) {
    statusNode.textContent = error.message;
  }
}

async function initializeDashboard() {
  try {
    const me = await fetchJson("/auth/me");
    document.getElementById("welcome-message").textContent = `Signed in as ${me.username}`;
    document.getElementById("connect-bank-button").addEventListener("click", connectBank);
    document.getElementById("logout-button").addEventListener("click", async () => {
      await fetchJson("/auth/logout", { method: "POST" });
      window.location.assign("/");
    });
    await loadAccounts();
  } catch (error) {
    window.location.assign("/");
  }
}

window.addEventListener("DOMContentLoaded", () => {
  const loginForm = document.getElementById("login-form");
  if (loginForm) {
    loginForm.addEventListener("submit", handleLogin);
    return;
  }

  if (document.getElementById("connect-bank-button")) {
    initializeDashboard();
  }
});
