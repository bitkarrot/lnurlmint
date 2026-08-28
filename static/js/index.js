window.PageLnurlmint = {
  template: '#page-lnurlmint',
  data() {
    return {
      mints: [],
      loading: false,
      errorMessage: '',
      createDialog: {
        show: false,
        loading: false,
        data: {
          username: ''
        }
      }
    }
  },
  mounted() {
    this.fetchMints()
  },
  methods: {
    openCreateDialog() {
      this.createDialog.data.username = ''
      this.createDialog.show = true
    },
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
    },
    async createMint() {
      this.createDialog.loading = true
      try {
        const wallet = this.g.user.wallets[0]
        await LNbits.api.request(
          'POST',
          '/lnurlmint/api/v1/mints',
          wallet.adminkey,
          this.createDialog.data
        )
        this.createDialog.show = false
        this.fetchMints()
      } catch (error) {
        LNbits.utils.notifyApiError(error)
        this.errorMessage = 'Failed to create mint'
      } finally {
        this.createDialog.loading = false
      }
    },
    async deleteMint(mint_id) {
      try {
        const wallet = this.g.user.wallets[0]
        await LNbits.api.request(
          'DELETE',
          '/lnurlmint/api/v1/mints/' + mint_id,
          wallet.adminkey
        )
        this.fetchMints()
      } catch (error) {
        if (error.response && error.response.status === 409) {
          this.errorMessage = 'Cannot delete mint with outstanding notes'
        } else {
          LNbits.utils.notifyApiError(error)
          this.errorMessage = 'Failed to delete mint'
        }
      }
    }
  }
}
