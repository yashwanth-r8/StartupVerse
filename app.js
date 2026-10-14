document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("input[type='number']").forEach((input) => {
    input.addEventListener("input", () => {
      if (input.value !== "" && Number(input.value) < 0) input.value = "0";
    });
  });
});
