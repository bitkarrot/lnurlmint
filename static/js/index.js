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
      this.loading = true
      try {
        const wallet = this.g.user.wallets[0]
        const key = wallet.inkey || wallet.adminkey
        const response = await LNbits.api.request(
          'GET',
          '/lnurlmint/api/v1/mints',
          key
        )
        this.mints = response.data || []
      } catch (error) {
        LNbits.utils.notifyApiError(error)
      } finally {
        this.loading = false
      }
    }
  }
}
