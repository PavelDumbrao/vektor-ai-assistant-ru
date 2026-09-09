(() => {
  const field = document.getElementById('sparkField');
  if (!field || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  const count = window.matchMedia('(max-width: 640px)').matches ? 14 : 24;
  for (let i = 0; i < count; i += 1) {
    const spark = document.createElement('i');
    spark.className = 'spark';
    spark.style.left = `${18 + Math.random() * 64}%`;
    spark.style.top = `${36 + Math.random() * 48}%`;
    spark.style.setProperty('--drift', `${-55 + Math.random() * 110}px`);
    spark.style.animationDuration = `${2.7 + Math.random() * 4.3}s`;
    spark.style.animationDelay = `${Math.random() * 5}s`;
    field.appendChild(spark);
  }
})();
