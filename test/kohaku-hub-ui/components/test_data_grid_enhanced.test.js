import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import { ElementPlusStubs } from "../helpers/vue";
import DataGridEnhanced from "@/components/DatasetViewer/DataGridEnhanced.vue";

describe("DataGridEnhanced", () => {
  it("shows image cells as thumbnails, AVIF and JFIF links included", () => {
    const wrapper = mount(DataGridEnhanced, {
      props: {
        columns: ["image", "caption"],
        rows: [
          ["https://cdn.example.com/posts/1.avif", "an AVIF"],
          ["https://cdn.example.com/posts/2.jfif?w=512", "a JFIF"],
          ["https://cdn.example.com/posts/3.mp4", "a video"],
          [null, 42],
        ],
      },
      global: { stubs: ElementPlusStubs },
    });

    const thumbs = wrapper.findAll('img[alt="Thumbnail"]').map((img) => img.attributes("src"));
    expect(thumbs).toEqual([
      "https://cdn.example.com/posts/1.avif",
      "https://cdn.example.com/posts/2.jfif?w=512",
    ]);
    expect(wrapper.text()).toContain("42");
  });
});
