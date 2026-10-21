import { createTheme, type MantineColorsTuple } from "@mantine/core";

// Petrol, from the Figma primitives (petrol/600 is the action colour at shade 6).
const petrol: MantineColorsTuple = [
  "#eef6f4",
  "#deeeea",
  "#b9d9d3",
  "#90c2bb",
  "#5e9e99",
  "#3c807f",
  "#246568",
  "#1c5356",
  "#284d4c",
  "#102b2d",
];

export const theme = createTheme({
  fontFamily: "'Manrope Variable', system-ui, sans-serif",
  headings: { fontFamily: "'Manrope Variable', system-ui, sans-serif", fontWeight: "600" },
  colors: { petrol },
  primaryColor: "petrol",
  primaryShade: 6,
  // 4 px for controls, chips and badges; 8 px for cards, panels and banners.
  radius: { xs: "4px", sm: "4px", md: "8px", lg: "12px", xl: "16px" },
  defaultRadius: "sm",
  spacing: { xs: "4px", sm: "8px", md: "12px", lg: "16px", xl: "24px" },
  fontSizes: { xs: "12px", sm: "14px", md: "16px", lg: "18px", xl: "24px" },
  lineHeights: { xs: "1.5", sm: "1.43", md: "1.5", lg: "1.44", xl: "1.33" },
  breakpoints: { xs: "36em", sm: "48em", md: "62em", lg: "75em", xl: "88em" },
  cursorType: "pointer",
  components: {
    Button: { defaultProps: { radius: "sm" } },
    TextInput: { defaultProps: { radius: "sm" } },
    SegmentedControl: { defaultProps: { radius: "sm" } },
    Popover: { defaultProps: { radius: "md", shadow: "md" } },
  },
});
