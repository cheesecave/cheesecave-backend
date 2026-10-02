<script setup>
/**
 * @typedef {Object} Props
 * @property {string} title - Card title
 * @property {number|string} value - Main value to display
 * @property {string} [subtitle] - Optional subtitle
 * @property {string} [icon] - Icon class (e.g., "i-carbon-user-multiple")
 * @property {string} [color] - Color theme (blue/purple/green/orange/cyan/pink)
 */

const props = defineProps({
  title: {
    type: String,
    required: true,
  },
  value: {
    type: [Number, String],
    required: true,
  },
  subtitle: {
    type: String,
    default: "",
  },
  icon: {
    type: String,
    default: "i-carbon-data-base",
  },
  color: {
    type: String,
    default: "blue",
    validator: (value) =>
      ["blue", "purple", "green", "orange", "cyan", "pink", "red"].includes(
        value,
      ),
  },
});

const colorClasses = {
  blue: "from-blue-500 to-blue-600",
  purple: "from-purple-500 to-purple-600",
  green: "from-green-500 to-green-600",
  orange: "from-orange-500 to-orange-600",
  cyan: "from-cyan-500 to-cyan-600",
  pink: "from-pink-500 to-pink-600",
  red: "from-red-500 to-red-600",
};
</script>

<template>
  <el-card shadow="hover" class="stat-card-wrapper">
    <div class="stat-card" :class="colorClasses[color]">
      <div :class="icon" class="stat-icon text-5xl mb-3 opacity-80" />
      <div class="stat-value text-5xl font-bold mb-2">{{ value }}</div>
      <div class="stat-title text-lg opacity-90 mb-2">
        {{ title }}
      </div>
      <div class="stat-subtitle text-sm opacity-75" :aria-hidden="!subtitle">
        {{ subtitle }}
      </div>
    </div>
  </el-card>
</template>

<style scoped>
.stat-card-wrapper {
  display: flex;
  min-width: 0;
  margin-bottom: 0;
  transition: transform 0.2s;
}

.stat-card-wrapper :deep(.el-card__body) {
  display: flex;
  flex: 1;
  min-width: 0;
}

.stat-card-wrapper:hover {
  transform: translateY(-4px);
}

.stat-card {
  container-type: inline-size;
  display: flex;
  flex: 1;
  flex-direction: column;
  align-items: center;
  min-width: 0;
  text-align: center;
  padding: 32px;
  border-radius: 8px;
  color: white;
  background: linear-gradient(
    135deg,
    var(--tw-gradient-from),
    var(--tw-gradient-to)
  );
}

.stat-icon {
  flex-shrink: 0;
}

.stat-value {
  font-size: clamp(2rem, 20cqi, 3rem);
  display: flex;
  flex: 1;
  align-items: center;
  justify-content: center;
  width: 100%;
  min-height: 2lh;
  overflow-wrap: anywhere;
}

.stat-title,
.stat-subtitle {
  width: 100%;
  min-height: 2lh;
  flex-shrink: 0;
  overflow-wrap: anywhere;
}

.from-blue-500 {
  --tw-gradient-from: #3b82f6;
}
.to-blue-600 {
  --tw-gradient-to: #2563eb;
}

.from-purple-500 {
  --tw-gradient-from: #a855f7;
}
.to-purple-600 {
  --tw-gradient-to: #9333ea;
}

.from-green-500 {
  --tw-gradient-from: #22c55e;
}
.to-green-600 {
  --tw-gradient-to: #16a34a;
}

.from-orange-500 {
  --tw-gradient-from: #f97316;
}
.to-orange-600 {
  --tw-gradient-to: #ea580c;
}

.from-cyan-500 {
  --tw-gradient-from: #06b6d4;
}
.to-cyan-600 {
  --tw-gradient-to: #0891b2;
}

.from-pink-500 {
  --tw-gradient-from: #ec4899;
}
.to-pink-600 {
  --tw-gradient-to: #db2777;
}

.from-red-500 {
  --tw-gradient-from: #ef4444;
}
.to-red-600 {
  --tw-gradient-to: #dc2626;
}
</style>
