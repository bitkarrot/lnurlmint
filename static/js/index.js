window.PageLnurlmint = {
  template: '#page-lnurlmint',
  data() {
    return {
      mints: [],
      loading: false
    }
  },
  mounted() {
    this.fetchMints()
  },
  methods: {
    async fetchMints() {
      // Stub for Phase 1 Task 1 — wired to the management API in Task 3.
      this.mints = []
    }
  }
}
